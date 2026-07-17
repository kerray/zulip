"use strict";

const assert = require("node:assert/strict");

const {clock, mock_esm, set_global, zrequire} = require("./lib/namespace.cjs");
const {run_test} = require("./lib/test.cjs");

const posted = [];
const deleted = [];
let post_should_fail = false;
let del_should_fail = false;

mock_esm("../src/channel", {
    post(opts) {
        posted.push(opts);
        if (post_should_fail) {
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

// A valid base64url string ("test"), and the raw bytes it decodes to; the
// browser reports the applicationServerKey of a subscription as those bytes.
const vapid_key = "dGVzdA";
const vapid_key_bytes = [116, 101, 115, 116];

let unsubscribe_count;

function make_subscription(overrides = {}) {
    return {
        endpoint: "https://push.example.com/abc",
        options: {applicationServerKey: new Uint8Array(vapid_key_bytes).buffer},
        getKey(name) {
            const bytes = name === "p256dh" ? [1, 2, 3, 4] : [5, 6, 7, 8];
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
const registration = {
    pushManager: {
        subscribe(options) {
            last_subscribe_options = options;
            // Model the browser: a fresh subscription is now what pushManager
            // reports.
            current_subscription = make_subscription();
            return Promise.resolve(current_subscription);
        },
        getSubscription: () => Promise.resolve(current_subscription),
    },
};

set_global("navigator", {
    userAgent: "TestAgent/1.0",
    serviceWorker: {
        register: () => Promise.resolve(registration),
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
    post_should_fail = false;
    del_should_fail = false;
    unsubscribe_count = 0;
    last_subscribe_options = undefined;
    current_subscription = make_subscription();
    Notification.permission = "granted";
    set_realm({server_web_push_vapid_public_key: vapid_key});
    initialize_user_settings({user_settings: {enable_web_push_notifications: true}});
}

run_test("subscribe posts subscription when enabled", async () => {
    reset();
    const effective = await web_push.handle_setting_change(true);
    assert.equal(effective, "applied");
    assert.equal(posted.length, 1);
    const {url, data} = posted[0];
    assert.equal(url, "/json/users/me/web_push_subscriptions");
    assert.equal(data.endpoint, "https://push.example.com/abc");
    assert.equal(data.user_agent, "TestAgent/1.0");
    // Keys are base64url-encoded with no padding.
    assert.match(data.p256dh_key, /^[\w-]+$/);
    assert.match(data.auth_secret, /^[\w-]+$/);
    assert.deepEqual(last_subscribe_options.userVisibleOnly, true);
    // The existing subscription already uses the current VAPID key, so it is
    // reused rather than dropped.
    assert.equal(unsubscribe_count, 0);
});

run_test("a stale VAPID key is dropped before re-subscribing", async () => {
    reset();
    // Model a subscription created under a since-rotated VAPID key.
    current_subscription = make_subscription({
        options: {applicationServerKey: new Uint8Array([9, 9, 9, 9]).buffer},
    });
    const effective = await web_push.handle_setting_change(true);
    assert.equal(effective, "applied");
    // The stale subscription is unsubscribed, then a fresh one is created with
    // the current key and posted to the server.
    assert.equal(unsubscribe_count, 1);
    assert.equal(posted.length, 1);
    assert.deepEqual([...last_subscribe_options.applicationServerKey], vapid_key_bytes);
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

run_test("initialize re-posts an existing subscription without creating one", async () => {
    reset();
    web_push.initialize();
    // initialize kicks off an async re-sync; let its promise chain settle.
    // Flush the fire-and-forget promise chain. The harness installs fake
    // timers, so advance the clock rather than awaiting a real setTimeout.
    await clock.tickAsync(0);
    // The existing subscription is re-posted to refresh the server's keys, but
    // no new subscription is created on startup.
    assert.equal(posted.length, 1);
    assert.equal(last_subscribe_options, undefined);
});

run_test("initialize enrolls a permission-granted browser that has no subscription", async () => {
    reset();
    current_subscription = null;
    // Setting on, no local subscription, permission already granted: this
    // browser is enrolled without a prompt (subscribe() cannot prompt when the
    // permission is already "granted").
    web_push.initialize();
    // Flush the fire-and-forget promise chain. The harness installs fake
    // timers, so advance the clock rather than awaiting a real setTimeout.
    await clock.tickAsync(0);
    assert.notEqual(last_subscribe_options, undefined);
    assert.equal(posted.length, 1);
});

run_test("initialize does not subscribe when permission is not yet granted", async () => {
    reset();
    current_subscription = null;
    Notification.permission = "default";
    web_push.initialize();
    // Flush the fire-and-forget promise chain. The harness installs fake
    // timers, so advance the clock rather than awaiting a real setTimeout.
    await clock.tickAsync(0);
    // Never trigger a permission prompt outside the settings-toggle gesture.
    assert.equal(posted.length, 0);
    assert.equal(last_subscribe_options, undefined);
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

run_test("initialize does nothing when the setting is off and no subscription exists", async () => {
    reset();
    current_subscription = null;
    initialize_user_settings({user_settings: {enable_web_push_notifications: false}});
    web_push.initialize();
    // Flush the fire-and-forget promise chain. The harness installs fake
    // timers, so advance the clock rather than awaiting a real setTimeout.
    await clock.tickAsync(0);
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
