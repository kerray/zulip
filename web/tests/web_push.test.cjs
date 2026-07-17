"use strict";

const assert = require("node:assert/strict");

const {clock, mock_esm, set_global, zrequire} = require("./lib/namespace.cjs");
const {run_test} = require("./lib/test.cjs");
const blueslip = require("./lib/zblueslip.cjs");

const posted = [];
const deleted = [];
let post_should_fail = false;
let del_should_fail = false;
// When post_should_park is set, channel.post holds the request instead of
// completing it and release_post() completes it later. That explicit deferred
// (never a timer) is how a test parks one operation inside the module's
// serialized queue while enqueuing more behind it.
let post_should_park = false;
let release_post;

mock_esm("../src/channel", {
    post(opts) {
        posted.push(opts);
        if (post_should_park) {
            release_post = () => {
                opts.success?.();
            };
        } else if (post_should_fail) {
            opts.error?.();
        } else {
            opts.success?.();
        }
    },
    del(opts) {
        deleted.push(opts);
        if (del_should_fail) {
            opts.error?.();
        } else {
            opts.success?.();
        }
    },
});

// Realistically shaped keys: a 65-byte uncompressed P-256 point for the VAPID
// application server key and for the subscription's p256dh key, and a 16-byte
// auth secret. Each was picked so that its standard-base64 form contains the
// "+", "/" and "=" characters base64url replaces or drops, and so that its
// base64url form contains "-" and "_" -- see the first test, which asserts
// exactly that. Toy keys hide both directions of the conversion: they encode
// to plain [A-Za-z0-9] either way, so a missing translation is invisible.
const vapid_key =
    "BJJslmF3LEoItv-A4QRcVPA7ppkjULtoVCEc2bIBbZuudeQS4s4nGyC26G5_MQw9ykJKcfjIGgXmxciOvQ3XodQ";
const p256dh_key =
    "BB4TVYszR_B8A33QfV1VCyXXDEz4UQNfD1o5LfMCuNJijxe44p8p8hdw_Nr9IQXDq4LTykvoCAPkRblAoO9-hho";
const auth_secret = "hXv-8lE51qTE6IKSlW_DmA";

// The raw bytes the browser reports for each, decoded with Node's own
// base64url support so that the fixtures do not depend on the code under test.
const vapid_key_bytes = [...Buffer.from(vapid_key, "base64url")];
const p256dh_bytes = [...Buffer.from(p256dh_key, "base64url")];
const auth_bytes = [...Buffer.from(auth_secret, "base64url")];

// The current VAPID key with its final byte flipped: a key of the same length,
// so a subscription bound to it is stale in a way only a byte-for-byte
// comparison notices.
const rotated_key_bytes = vapid_key_bytes.map((byte, index) =>
    index === vapid_key_bytes.length - 1 ? 255 - byte : byte,
);

let unsubscribe_count;

function make_subscription(overrides = {}) {
    return {
        endpoint: "https://push.example.com/abc",
        options: {applicationServerKey: new Uint8Array(vapid_key_bytes).buffer},
        getKey(name) {
            const bytes = name === "p256dh" ? p256dh_bytes : auth_bytes;
            return new Uint8Array(bytes).buffer;
        },
        unsubscribe() {
            unsubscribe_count += 1;
            // Model the browser: after unsubscribing, pushManager no longer
            // reports a subscription.
            current_subscription = null;
            return Promise.resolve(true);
        },
        ...overrides,
    };
}

// The subscription pushManager.getSubscription() currently reports; tests set
// this to null to model a browser that has never subscribed.
let current_subscription;
let last_subscribe_options;
// Service worker calls, recorded so that a test asserting nothing HAPPENED can
// still prove the async chain ran at all: a counter that stayed at zero is
// equally satisfied by a chain that never started.
const registered = [];
let get_subscription_count;
const registration = {
    pushManager: {
        subscribe(options) {
            last_subscribe_options = options;
            // Model the browser: a fresh subscription is now what pushManager
            // reports.
            current_subscription = make_subscription();
            return Promise.resolve(current_subscription);
        },
        getSubscription() {
            get_subscription_count += 1;
            return Promise.resolve(current_subscription);
        },
    },
};

set_global("navigator", {
    userAgent: "TestAgent/1.0",
    serviceWorker: {
        register(url, options) {
            registered.push({url, options});
            return Promise.resolve(registration);
        },
        ready: Promise.resolve(registration),
        getRegistration: () => Promise.resolve(registration),
    },
});

// is_supported checks for these keys on window.
set_global("window", {
    PushManager() {},
    Notification: {},
});

set_global("Notification", {
    permission: "granted",
    requestPermission: () => Promise.resolve("granted"),
});

const {set_realm} = zrequire("state_data");
const {initialize_user_settings} = zrequire("user_settings");
const web_push = zrequire("web_push");

function reset() {
    posted.length = 0;
    deleted.length = 0;
    registered.length = 0;
    post_should_fail = false;
    del_should_fail = false;
    post_should_park = false;
    release_post = undefined;
    unsubscribe_count = 0;
    get_subscription_count = 0;
    last_subscribe_options = undefined;
    current_subscription = make_subscription();
    Notification.permission = "granted";
    set_realm({server_web_push_vapid_public_key: vapid_key});
    initialize_user_settings({user_settings: {enable_web_push_notifications: true}});
}

run_test("the test keys exercise both directions of the base64url conversion", () => {
    // Assert the property the rest of the file relies on, so that swapping in
    // a tamer key fails here instead of quietly testing nothing: standard
    // base64 spells these bytes with characters that are invalid in base64url
    // (which is what the server's validator accepts), and the base64url form
    // contains the substitutes that have to be translated back before
    // decoding.
    for (const [bytes, encoded] of [
        [vapid_key_bytes, vapid_key],
        [p256dh_bytes, p256dh_key],
        [auth_bytes, auth_secret],
    ]) {
        const standard_base64 = Buffer.from(bytes).toString("base64");
        assert.ok(standard_base64.includes("+"));
        assert.ok(standard_base64.includes("/"));
        assert.ok(standard_base64.includes("="));
        assert.ok(encoded.includes("-"));
        assert.ok(encoded.includes("_"));
        assert.ok(!encoded.includes("="));
    }
});

run_test("subscribe posts subscription when enabled", async () => {
    reset();
    const effective = await web_push.handle_setting_change(true);
    assert.equal(effective, "applied");
    assert.equal(posted.length, 1);
    const {url, data} = posted[0];
    assert.equal(url, "/json/users/me/web_push_subscriptions");
    assert.equal(data.endpoint, "https://push.example.com/abc");
    assert.equal(data.user_agent, "TestAgent/1.0");
    // Keys are base64url-encoded with no padding. The server rejects anything
    // else, so assert the exact strings rather than their general shape.
    assert.equal(data.p256dh_key, p256dh_key);
    assert.equal(data.auth_secret, auth_secret);
    assert.deepEqual(last_subscribe_options.userVisibleOnly, true);
    // The VAPID key reaches the browser as the bytes it decodes to.
    assert.deepEqual([...last_subscribe_options.applicationServerKey], vapid_key_bytes);
    // The existing subscription already uses the current VAPID key, so it is
    // reused rather than dropped.
    assert.equal(unsubscribe_count, 0);
});

run_test("a subscription with no keys is not posted", async () => {
    reset();
    // A subscription the browser will not hand us keys for cannot be encrypted
    // to, so registering its endpoint would only create an undeliverable row.
    current_subscription = make_subscription({
        getKey: (name) => (name === "auth" ? null : new Uint8Array(p256dh_bytes).buffer),
    });
    web_push.initialize();
    // Flush the fire-and-forget promise chain. The harness installs fake
    // timers, so advance the clock rather than awaiting a real setTimeout.
    await clock.tickAsync(0);
    assert.equal(get_subscription_count, 1);
    assert.equal(posted.length, 0);
});

run_test("a stale VAPID key is dropped before re-subscribing", async () => {
    reset();
    // Model a subscription created under a since-rotated VAPID key.
    current_subscription = make_subscription({
        options: {applicationServerKey: new Uint8Array(rotated_key_bytes).buffer},
    });
    const effective = await web_push.handle_setting_change(true);
    assert.equal(effective, "applied");
    // The stale subscription is unsubscribed, then a fresh one is created with
    // the current key and posted to the server.
    assert.equal(unsubscribe_count, 1);
    assert.equal(posted.length, 1);
    assert.deepEqual([...last_subscribe_options.applicationServerKey], vapid_key_bytes);
});

run_test("a subscription key that does not match exactly is stale", async () => {
    for (const application_server_key of [
        // The browser reports no key at all for this subscription, so we
        // cannot tell it apart from one bound to a key we have rotated away
        // from; treat it as stale.
        null,
        // A prefix of the current key: every byte we can compare matches, so
        // only comparing the lengths too rejects it.
        new Uint8Array(vapid_key_bytes.slice(0, 32)).buffer,
    ]) {
        reset();
        current_subscription = make_subscription({
            options: {applicationServerKey: application_server_key},
        });
        const effective = await web_push.handle_setting_change(true);
        assert.equal(effective, "applied");
        assert.equal(unsubscribe_count, 1);
        assert.equal(posted.length, 1);
        assert.deepEqual([...last_subscribe_options.applicationServerKey], vapid_key_bytes);
    }
});

run_test("long user agent is truncated to the server's 255-char cap", async () => {
    reset();
    const original_user_agent = navigator.userAgent;
    navigator.userAgent = "A".repeat(300);
    await web_push.handle_setting_change(true);
    navigator.userAgent = original_user_agent;
    assert.equal(posted.length, 1);
    assert.equal(posted[0].data.user_agent.length, 255);
});

run_test("no VAPID key short-circuits", async () => {
    reset();
    set_realm({server_web_push_vapid_public_key: ""});
    const effective = await web_push.handle_setting_change(true);
    // Nothing to gate on: the setting may be persisted even though this
    // server has web push unconfigured.
    assert.equal(effective, "applied");
    assert.equal(posted.length, 0);
});

run_test("the settings UI can ask what the server and this browser support", async () => {
    reset();
    assert.ok(web_push.is_web_push_configured());
    assert.ok(await web_push.has_local_subscription());

    current_subscription = null;
    assert.ok(!(await web_push.has_local_subscription()));

    set_realm({server_web_push_vapid_public_key: ""});
    const asked_so_far = get_subscription_count;
    assert.ok(!web_push.is_web_push_configured());
    // A server with no VAPID key cannot have enrolled this browser, so we
    // answer without consulting the service worker at all.
    assert.ok(!(await web_push.has_local_subscription()));
    assert.equal(get_subscription_count, asked_so_far);
});

run_test("denied permission returns false and does not subscribe", async () => {
    reset();
    const original_request_permission = Notification.requestPermission;
    Notification.requestPermission = () => Promise.resolve("denied");
    const effective = await web_push.handle_setting_change(true);
    Notification.requestPermission = original_request_permission;
    assert.equal(effective, "permission-denied");
    assert.equal(posted.length, 0);
});

run_test("a failed server registration rejects so the caller can revert", async () => {
    reset();
    post_should_fail = true;
    await assert.rejects(web_push.handle_setting_change(true));
});

run_test("disabling deletes the server row before unsubscribing the browser", async () => {
    reset();
    let deleted_before_unsubscribe = false;
    current_subscription = make_subscription({
        unsubscribe() {
            // The server DELETE (pushed to `deleted`) must already have run.
            deleted_before_unsubscribe = deleted.length === 1;
            unsubscribe_count += 1;
            return Promise.resolve(true);
        },
    });
    const effective = await web_push.handle_setting_change(false);
    assert.equal(effective, "applied");
    assert.equal(unsubscribe_count, 1);
    assert.equal(deleted.length, 1);
    assert.equal(deleted[0].data.endpoint, "https://push.example.com/abc");
    // A failed delete must never strand a browser with no server row, so the
    // delete has to happen first (see the next test for the failure path).
    assert.ok(deleted_before_unsubscribe);
});

run_test("a failed server delete rejects and leaves the browser subscribed", async () => {
    reset();
    del_should_fail = true;
    await assert.rejects(web_push.handle_setting_change(false));
    // The browser is NOT unsubscribed, so the toggle can revert consistently.
    assert.equal(unsubscribe_count, 0);
});

run_test("a browser that refuses to unsubscribe still counts as disabled", async () => {
    reset();
    current_subscription = make_subscription({
        unsubscribe: () => Promise.reject(new Error("browser said no")),
    });
    blueslip.expect("warn", "Failed to unsubscribe browser from web push");
    const effective = await web_push.handle_setting_change(false);
    // The server row is already gone, so no notification can be sent here
    // regardless. Failing the toggle now would only revert a change the server
    // has already accepted.
    assert.equal(effective, "applied");
    assert.equal(deleted.length, 1);
});

run_test("disabling is a no-op when this browser holds no subscription", async () => {
    reset();
    current_subscription = null;
    const effective = await web_push.handle_setting_change(false);
    assert.equal(effective, "applied");
    // There is no endpoint to delete, so we must not ask the server to delete
    // one.
    assert.equal(get_subscription_count, 1);
    assert.equal(deleted.length, 0);
    assert.equal(unsubscribe_count, 0);
});

run_test("initialize re-posts an existing subscription without creating one", async () => {
    reset();
    web_push.initialize();
    // initialize kicks off an async re-sync; let its promise chain settle.
    // Flush the fire-and-forget promise chain. The harness installs fake
    // timers, so advance the clock rather than awaiting a real setTimeout.
    await clock.tickAsync(0);
    // The service worker is registered at the root scope, so it receives push
    // events for the whole origin.
    assert.deepEqual(registered, [{url: "/service-worker.js", options: {scope: "/"}}]);
    // The existing subscription is re-posted to refresh the server's keys, but
    // no new subscription is created on startup.
    assert.equal(get_subscription_count, 1);
    assert.equal(posted.length, 1);
    assert.equal(last_subscribe_options, undefined);
});

run_test("initialize does nothing when the server has no VAPID key", async () => {
    reset();
    set_realm({server_web_push_vapid_public_key: ""});
    web_push.initialize();
    // Flush the fire-and-forget promise chain. The harness installs fake
    // timers, so advance the clock rather than awaiting a real setTimeout.
    await clock.tickAsync(0);
    // Not even the service worker is registered: there is nothing it could do.
    assert.deepEqual(registered, []);
    assert.equal(get_subscription_count, 0);
    assert.equal(posted.length, 0);
});

run_test("a failed load-time re-post is logged rather than left unhandled", async () => {
    reset();
    post_should_fail = true;
    blueslip.expect("warn", "Failed to sync web push subscription");
    web_push.initialize();
    // Flush the fire-and-forget promise chain. The harness installs fake
    // timers, so advance the clock rather than awaiting a real setTimeout.
    await clock.tickAsync(0);
    assert.equal(posted.length, 1);
});

run_test("initialize does not enroll a browser that holds no subscription", async () => {
    reset();
    current_subscription = null;
    // The setting is on and this browser has already granted the notification
    // permission -- but for desktop notifications, as far as we can tell. The
    // account-wide setting does not say the user wants notifications HERE, so
    // this browser is left unenrolled until the user asks for it in it.
    web_push.initialize();
    // Flush the fire-and-forget promise chain. The harness installs fake
    // timers, so advance the clock rather than awaiting a real setTimeout.
    await clock.tickAsync(0);
    // The sync really ran and looked at this browser's state; it just declined
    // to enroll it.
    assert.equal(registered.length, 1);
    assert.equal(get_subscription_count, 1);
    assert.equal(last_subscribe_options, undefined);
    assert.equal(posted.length, 0);
    assert.equal(current_subscription, null);
});

run_test("initialize does not subscribe when permission is not yet granted", async () => {
    reset();
    current_subscription = null;
    Notification.permission = "default";
    web_push.initialize();
    // Flush the fire-and-forget promise chain. The harness installs fake
    // timers, so advance the clock rather than awaiting a real setTimeout.
    await clock.tickAsync(0);
    // The sync ran to the point of inspecting this browser ...
    assert.equal(registered.length, 1);
    assert.equal(get_subscription_count, 1);
    // ... and then never triggered a permission prompt outside the
    // settings-toggle gesture.
    assert.equal(posted.length, 0);
    assert.equal(last_subscribe_options, undefined);
});

run_test("initialize re-subscribes a browser left on a rotated VAPID key", async () => {
    reset();
    // Model a browser still holding a subscription from before the server's
    // VAPID key was rotated. The push service rejects sends to it with 403,
    // which does not prune the server-side row, so merely re-posting it would
    // leave this browser permanently undeliverable.
    current_subscription = make_subscription({
        options: {applicationServerKey: new Uint8Array(rotated_key_bytes).buffer},
    });
    web_push.initialize();
    // Flush the fire-and-forget promise chain. The harness installs fake
    // timers, so advance the clock rather than awaiting a real setTimeout.
    await clock.tickAsync(0);
    // The stale subscription is dropped and recreated against the current key,
    // and only that fresh subscription is posted.
    assert.equal(unsubscribe_count, 1);
    assert.deepEqual([...last_subscribe_options.applicationServerKey], vapid_key_bytes);
    assert.equal(posted.length, 1);
    assert.equal(posted[0].data.endpoint, "https://push.example.com/abc");
});

run_test("initialize leaves a rotated-key subscription alone without permission", async () => {
    reset();
    Notification.permission = "default";
    current_subscription = make_subscription({
        options: {applicationServerKey: new Uint8Array(rotated_key_bytes).buffer},
    });
    web_push.initialize();
    // Flush the fire-and-forget promise chain. The harness installs fake
    // timers, so advance the clock rather than awaiting a real setTimeout.
    await clock.tickAsync(0);
    // The sync ran and saw the stale subscription ...
    assert.equal(registered.length, 1);
    assert.equal(get_subscription_count, 1);
    // ... but re-subscribing would prompt outside a user gesture, so this
    // browser waits to be re-enrolled from the settings banner instead. The
    // dead subscription is not re-posted either.
    assert.equal(unsubscribe_count, 0);
    assert.equal(last_subscribe_options, undefined);
    assert.equal(posted.length, 0);
});

run_test("initialize destroys a lingering subscription when the setting is off", async () => {
    reset();
    initialize_user_settings({user_settings: {enable_web_push_notifications: false}});
    web_push.initialize();
    // Flush the fire-and-forget promise chain. The harness installs fake
    // timers, so advance the clock rather than awaiting a real setTimeout.
    await clock.tickAsync(0);
    // A subscription left by a previous account holder must be torn down: the
    // browser unsubscribes (invalidating the endpoint) and we best-effort
    // delete any server row that now belongs to the current user.
    assert.equal(unsubscribe_count, 1);
    assert.equal(deleted.length, 1);
    assert.equal(posted.length, 0);
});

run_test("a failed cleanup delete does not abort the teardown", async () => {
    reset();
    initialize_user_settings({user_settings: {enable_web_push_notifications: false}});
    del_should_fail = true;
    blueslip.expect("warn", "Failed to delete stale web push subscription");
    web_push.initialize();
    // Flush the fire-and-forget promise chain. The harness installs fake
    // timers, so advance the clock rather than awaiting a real setTimeout.
    await clock.tickAsync(0);
    // Unsubscribing the browser is what actually stops delivery -- it
    // invalidates the endpoint, so the server's row is pruned by the next
    // send. A server row that may not even exist is best-effort.
    assert.equal(unsubscribe_count, 1);
    assert.equal(deleted.length, 1);
});

run_test("initialize does nothing when the setting is off and no subscription exists", async () => {
    reset();
    current_subscription = null;
    initialize_user_settings({user_settings: {enable_web_push_notifications: false}});
    web_push.initialize();
    // Flush the fire-and-forget promise chain. The harness installs fake
    // timers, so advance the clock rather than awaiting a real setTimeout.
    await clock.tickAsync(0);
    assert.equal(get_subscription_count, 1);
    assert.equal(unsubscribe_count, 0);
    assert.equal(deleted.length, 0);
    assert.equal(posted.length, 0);
});

run_test("a queued load-sync after a disable does not resurrect the subscription", async () => {
    reset();
    // This browser is subscribed and web push starts on.
    current_subscription = make_subscription();
    Notification.permission = "granted";

    // The user disables web push: this enqueues the unsubscribe (delete the
    // server row, then unsubscribe the browser) ...
    const disable = web_push.handle_setting_change(false);
    // ... and the setting is persisted off.
    initialize_user_settings({user_settings: {enable_web_push_notifications: false}});
    // A load-time sync is enqueued right behind the disable. Because every
    // mutating operation shares one serialized chain, it runs AFTER the disable
    // finishes and re-reads the now-off setting and the (now absent)
    // subscription when it runs, so it neither re-posts the old subscription
    // nor creates a new one. Without serialization the load sync could race the
    // disable and leave the setting off with a live subscription.
    web_push.initialize();

    await disable;
    // Flush the fire-and-forget promise chain. The harness installs fake
    // timers, so advance the clock rather than awaiting a real setTimeout.
    await clock.tickAsync(0);

    // The server row was deleted exactly once and never re-registered; the
    // browser is left unsubscribed.
    assert.equal(deleted.length, 1);
    assert.equal(posted.length, 0);
    assert.equal(last_subscribe_options, undefined);
    assert.equal(current_subscription, null);
});

run_test("a load-sync superseded before it runs stands down", async () => {
    reset();
    // Park an operation in the queue: this toggle's server POST hangs until the
    // test releases it, so everything enqueued behind it waits.
    post_should_park = true;
    const parked = web_push.handle_setting_change(true);
    await clock.tickAsync(0);
    post_should_park = false;
    // The parked operation really is in flight and holding the queue.
    assert.equal(posted.length, 1);

    // A load-time sync is enqueued behind it. On its own it would re-post this
    // browser's subscription ...
    web_push.initialize();
    // ... but the user toggles again first, and that toggle's own operation
    // fully expresses the current desired state. The load-sync carries no
    // intent of its own, so it must not run on top of a newer one.
    const enable = web_push.handle_setting_change(true);

    release_post();
    assert.equal(await parked, "applied");
    assert.equal(await enable, "applied");
    await clock.tickAsync(0);

    // One post per toggle: the load-sync that ran between them contributed
    // none of its own.
    assert.equal(posted.length, 2);
});

run_test("a delayed permission grant does not resurrect a disabled subscription", async () => {
    reset();
    // This browser is subscribed and web push starts on.
    current_subscription = make_subscription();

    // Model a slow permission prompt: requestPermission stays pending until the
    // test resolves it, mimicking the user leaving the browser prompt open.
    const original_request_permission = Notification.requestPermission;
    let grant_permission;
    Notification.permission = "default";
    Notification.requestPermission = () =>
        new Promise((resolve) => {
            grant_permission = () => {
                resolve("granted");
            };
        });

    // The user toggles ON: the permission prompt opens and blocks BEFORE any
    // subscribe is enqueued.
    const enable = web_push.handle_setting_change(true);

    // While the prompt is still open, the user toggles OFF. The disable runs to
    // completion (deletes the server row, unsubscribes the browser) and the
    // setting is persisted off.
    const disable = web_push.handle_setting_change(false);
    initialize_user_settings({user_settings: {enable_web_push_notifications: false}});
    const disable_effective = await disable;
    assert.equal(disable_effective, "applied");
    assert.equal(deleted.length, 1);
    assert.equal(current_subscription, null);

    // Now the still-open prompt is granted. The enable's queued subscribe must
    // recognize it was superseded by the later disable and do nothing, rather
    // than resurrecting a subscription the user just turned off.
    grant_permission();
    const enable_effective = await enable;
    Notification.requestPermission = original_request_permission;

    assert.equal(enable_effective, "superseded");
    // No subscription was created and nothing was posted to the server.
    assert.equal(posted.length, 0);
    assert.equal(last_subscribe_options, undefined);
    assert.equal(current_subscription, null);
});

run_test("a permission decision arriving after a later toggle is superseded", async () => {
    reset();
    // Model a slow permission prompt that the user eventually DENIES, long
    // after they have moved on.
    const original_request_permission = Notification.requestPermission;
    let deny_permission;
    Notification.permission = "default";
    Notification.requestPermission = () =>
        new Promise((resolve) => {
            deny_permission = () => {
                resolve("denied");
            };
        });

    // The user toggles ON, then changes their mind and toggles OFF while the
    // prompt is still open. The disable settles the question.
    const enable = web_push.handle_setting_change(true);
    const disable = web_push.handle_setting_change(false);
    assert.equal(await disable, "applied");

    deny_permission();
    const enable_effective = await enable;
    Notification.requestPermission = original_request_permission;

    // The stale toggle reports that it was superseded rather than that
    // permission was denied: the caller must not revert the setting the newer
    // toggle owns, nor explain a permission problem that no longer describes
    // anything the user is waiting on.
    assert.equal(enable_effective, "superseded");
    assert.equal(posted.length, 0);
});

run_test("a subscribe superseded while already queued does not run", async () => {
    reset();
    // The permission is already granted, so the enable below never waits on a
    // prompt: it clears the pre-queue check and takes its place in the queue
    // straight away. That is what makes the later toggle overtake it INSIDE
    // the queue rather than while it waits on the prompt.
    Notification.permission = "granted";

    // Park an operation in the queue: this toggle's server POST hangs until the
    // test releases it, so everything enqueued behind it waits.
    post_should_park = true;
    const parked = web_push.handle_setting_change(true);
    await clock.tickAsync(0);
    post_should_park = false;
    // The parked operation really is in flight and holding the queue.
    assert.equal(posted.length, 1);

    // A second enable gets past the permission prompt and into the queue ...
    const superseded = web_push.handle_setting_change(true);
    await clock.tickAsync(0);
    // ... where it is still waiting, behind the parked operation.
    assert.equal(posted.length, 1);

    // Only now does the user turn web push off, bumping the desired-state
    // token while that subscribe sits in the queue.
    const disable = web_push.handle_setting_change(false);

    release_post();
    assert.equal(await parked, "applied");
    assert.equal(await superseded, "superseded");
    assert.equal(await disable, "applied");

    // The queued subscribe found a newer intent when it finally ran and did
    // nothing, so the disable's teardown is what stands.
    assert.equal(posted.length, 1);
    assert.equal(deleted.length, 1);
    assert.equal(unsubscribe_count, 1);
    assert.equal(current_subscription, null);
});

run_test("of two disables enqueued together only the later one acts", async () => {
    reset();
    // Both toggles enqueue before either operation runs, so the first one finds
    // a newer intent when it executes and reports itself superseded instead of
    // repeating work the second one is about to do.
    const superseded = web_push.handle_setting_change(false);
    const latest = web_push.handle_setting_change(false);

    assert.equal(await superseded, "superseded");
    assert.equal(await latest, "applied");
    assert.equal(deleted.length, 1);
    assert.equal(unsubscribe_count, 1);
});
