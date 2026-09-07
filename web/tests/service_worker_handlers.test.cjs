"use strict";

const assert = require("node:assert/strict");

const {zrequire} = require("./lib/namespace.cjs");
const {run_test} = require("./lib/test.cjs");

const {handle_notification_click, handle_push, parse_message_payload} =
    zrequire("service_worker_handlers");

const REALM_URL = "https://chat.example.com";

function make_payload(overrides = {}) {
    return {
        type: "message",
        message_id: 42,
        realm_url: REALM_URL,
        realm_name: "Example",
        title: "King Hamlet",
        body: "hello",
        icon: `${REALM_URL}/avatar/10`,
        badge: `${REALM_URL}/static/images/logo/zulip-icon-192x192.png`,
        tag: "message:42",
        url: `${REALM_URL}/#narrow/dm/10-Hamlet/near/42`,
        ...overrides,
    };
}

// A stub of the parts of ServiceWorkerGlobalScope the handlers touch. Each
// call is recorded so a test can assert on what the worker did.
function make_scope({clients = [], open_window_fails = false} = {}) {
    const shown = [];
    const opened = [];
    const match_all_options = [];
    return {
        shown,
        opened,
        match_all_options,
        clients: {
            matchAll(options) {
                match_all_options.push(options);
                return Promise.resolve(clients);
            },
            openWindow(url) {
                opened.push(url);
                return open_window_fails ? Promise.reject(new Error("blocked")) : Promise.resolve();
            },
        },
        registration: {
            showNotification(title, options) {
                shown.push({title, options});
                return Promise.resolve();
            },
        },
    };
}

function make_client({
    url = `${REALM_URL}/`,
    visibilityState = "hidden",
    can_navigate = true,
} = {}) {
    const client = {url, visibilityState, focused: false, navigated: []};
    client.focus = () => {
        client.focused = true;
        return Promise.resolve(client);
    };
    client.navigate = (target) => {
        if (!can_navigate) {
            return Promise.reject(new Error("not controlled"));
        }
        client.navigated.push(target);
        return Promise.resolve(client);
    };
    return client;
}

run_test("parse_message_payload rejects anything but our message payload", () => {
    assert.equal(parse_message_payload(undefined), undefined);
    assert.equal(parse_message_payload(null), undefined);
    assert.equal(parse_message_payload("a string"), undefined);
    assert.equal(parse_message_payload(42), undefined);
    assert.equal(parse_message_payload({}), undefined);
    assert.equal(parse_message_payload({type: "remove"}), undefined);

    const payload = make_payload();
    assert.deepEqual(parse_message_payload(payload), payload);
});

run_test("a push with no visible client shows the notification", async () => {
    const scope = make_scope({clients: [make_client({visibilityState: "hidden"})]});
    await handle_push(make_payload(), scope);

    assert.equal(scope.shown.length, 1);
    assert.equal(scope.shown[0].title, "King Hamlet");
    assert.equal(scope.shown[0].options.body, "hello");
    assert.equal(scope.shown[0].options.tag, "message:42");
    assert.deepEqual(scope.shown[0].options.data, {
        url: `${REALM_URL}/#narrow/dm/10-Hamlet/near/42`,
    });
});

run_test("a visible client suppresses the notification", async () => {
    const scope = make_scope({clients: [make_client({visibilityState: "visible"})]});
    await handle_push(make_payload(), scope);

    assert.equal(scope.shown.length, 0);
});

run_test("uncontrolled clients are consulted when suppressing", async () => {
    // A tab loaded before this worker took control is still showing the user
    // their messages. Without includeUncontrolled it would be invisible to
    // matchAll and we would double-notify.
    const scope = make_scope({clients: [make_client({visibilityState: "visible"})]});
    await handle_push(make_payload(), scope);

    assert.equal(scope.match_all_options.length, 1);
    assert.equal(scope.match_all_options[0].includeUncontrolled, true);
});

run_test("a malformed push payload shows nothing", async () => {
    const scope = make_scope();
    await handle_push({type: "something-else"}, scope);
    await handle_push(undefined, scope);

    assert.equal(scope.shown.length, 0);
    // We never even ask about clients for a payload we cannot render.
    assert.equal(scope.match_all_options.length, 0);
});

run_test("clicking a notification focuses and navigates a same-origin client", async () => {
    const client = make_client();
    const scope = make_scope({clients: [client]});
    const url = `${REALM_URL}/#narrow/dm/10-Hamlet/near/42`;
    await handle_notification_click({url}, scope);

    assert.ok(client.focused);
    assert.deepEqual(client.navigated, [url]);
    assert.equal(scope.opened.length, 0);
});

run_test("a cross-origin client is not reused", async () => {
    // Another site's tab must never be focused or navigated to our URL.
    const other = make_client({url: "https://evil.example.com/"});
    const scope = make_scope({clients: [other]});
    const url = `${REALM_URL}/#narrow/dm/10-Hamlet/near/42`;
    await handle_notification_click({url}, scope);

    assert.ok(!other.focused);
    assert.deepEqual(other.navigated, []);
    assert.deepEqual(scope.opened, [url]);
});

run_test("a client that cannot be navigated falls back to a new window", async () => {
    const client = make_client({can_navigate: false});
    const scope = make_scope({clients: [client]});
    const url = `${REALM_URL}/#narrow/dm/10-Hamlet/near/42`;
    await handle_notification_click({url}, scope);

    assert.ok(client.focused);
    assert.deepEqual(client.navigated, []);
    assert.deepEqual(scope.opened, [url]);
});

run_test("with no open client a new window is opened", async () => {
    const scope = make_scope({clients: []});
    const url = `${REALM_URL}/#narrow/dm/10-Hamlet/near/42`;
    await handle_notification_click({url}, scope);

    assert.deepEqual(scope.opened, [url]);
});

run_test("a notification without a url does nothing", async () => {
    const scope = make_scope({clients: [make_client()]});
    await handle_notification_click(undefined, scope);
    await handle_notification_click({}, scope);
    await handle_notification_click({url: 42}, scope);

    assert.equal(scope.opened.length, 0);
    assert.equal(scope.match_all_options.length, 0);
});
