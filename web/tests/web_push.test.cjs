"use strict";

const assert = require("node:assert/strict");

const {mock_esm, set_global, zrequire} = require("./lib/namespace.cjs");
const {run_test} = require("./lib/test.cjs");

const posted = [];
const deleted = [];

mock_esm("../src/channel", {
    post(opts) {
        posted.push(opts);
    },
    del(opts) {
        deleted.push(opts);
    },
});

function make_subscription() {
    return {
        endpoint: "https://push.example.com/abc",
        getKey(name) {
            const bytes = name === "p256dh" ? [1, 2, 3, 4] : [5, 6, 7, 8];
            return new Uint8Array(bytes).buffer;
        },
        unsubscribe: () => Promise.resolve(true),
    };
}

let last_subscribe_options;
const registration = {
    pushManager: {
        subscribe(options) {
            last_subscribe_options = options;
            return Promise.resolve(make_subscription());
        },
        getSubscription: () => Promise.resolve(make_subscription()),
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

// A valid base64url string ("test").
const vapid_key = "dGVzdA";

function reset() {
    posted.length = 0;
    deleted.length = 0;
    last_subscribe_options = undefined;
    set_realm({server_web_push_vapid_public_key: vapid_key});
    initialize_user_settings({user_settings: {enable_web_push_notifications: true}});
}

run_test("subscribe posts subscription when enabled", async () => {
    reset();
    await web_push.handle_setting_change(true);
    assert.equal(posted.length, 1);
    const {url, data} = posted[0];
    assert.equal(url, "/json/users/me/web_push_subscriptions");
    assert.equal(data.endpoint, "https://push.example.com/abc");
    assert.equal(data.user_agent, "TestAgent/1.0");
    // Keys are base64url-encoded with no padding.
    assert.match(data.p256dh_key, /^[\w-]+$/);
    assert.match(data.auth_secret, /^[\w-]+$/);
    assert.deepEqual(last_subscribe_options.userVisibleOnly, true);
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
    await web_push.handle_setting_change(true);
    assert.equal(posted.length, 0);
});

run_test("disabling unsubscribes", async () => {
    reset();
    await web_push.handle_setting_change(false);
    assert.equal(deleted.length, 1);
    assert.equal(deleted[0].data.endpoint, "https://push.example.com/abc");
});

run_test("initialize subscribes when permission already granted", async () => {
    reset();
    web_push.initialize();
    // initialize kicks off an async subscribe; let its promise chain settle.
    await new Promise((resolve) => {
        setTimeout(resolve, 0);
    });
    assert.equal(posted.length, 1);
});
