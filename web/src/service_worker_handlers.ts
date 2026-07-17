/// <reference lib="webworker" />

// The behavior behind the service worker's push and notificationclick
// listeners, separated from service-worker.ts so it can be unit tested: these
// functions take the ServiceWorkerGlobalScope they act on as a parameter
// rather than reaching for the ambient `self`, so a test can pass a stub.
//
// They render the OS notification from the compact JSON payload built by
// get_message_payload_webpush in zerver/lib/push_notifications.py.

type MessagePayload = {
    type: "message";
    message_id: number;
    realm_url: string;
    realm_name: string;
    title: string;
    body: string;
    icon: string;
    badge: string;
    tag: string;
    // An absolute `near/<id>` narrow URL, built server-side. We do not
    // reconstruct it here: Zulip's hash encoding is not encodeURIComponent, so
    // topics with "." or non-ASCII would be mis-encoded.
    url: string;
};

function is_record(value: unknown): value is Record<string, unknown> {
    return typeof value === "object" && value !== null;
}

export function parse_message_payload(data: unknown): MessagePayload | undefined {
    if (!is_record(data) || data["type"] !== "message") {
        return undefined;
    }
    // Trust boundary: this payload is authored by our own server (see
    // get_message_payload_webpush), not user input, so we trust its shape after
    // the type tag check above rather than validating every field at runtime.
    // eslint-disable-next-line @typescript-eslint/consistent-type-assertions
    return data as MessagePayload;
}

export async function handle_push(data: unknown, scope: ServiceWorkerGlobalScope): Promise<void> {
    const payload = parse_message_payload(data);
    if (payload === undefined) {
        return;
    }
    // When a Zulip tab is visible, the page itself already shows an in-app
    // notification for this message (see message_notifications.ts), so
    // rendering an OS notification here too would double-notify. With
    // userVisibleOnly subscriptions browsers tolerate skipping the OS
    // notification as long as a visible client is handling it.
    //
    // includeUncontrolled matters here: a tab loaded before this worker took
    // control is still showing the user their messages, and omitting it would
    // make such a tab invisible to us and double-notify.
    const clients = await scope.clients.matchAll({type: "window", includeUncontrolled: true});
    if (clients.some((client) => client.visibilityState === "visible")) {
        return;
    }
    await scope.registration.showNotification(payload.title, {
        body: payload.body,
        icon: payload.icon,
        badge: payload.badge,
        tag: payload.tag,
        data: {url: payload.url},
    });
}

export async function handle_notification_click(
    data: unknown,
    scope: ServiceWorkerGlobalScope,
): Promise<void> {
    if (!is_record(data) || typeof data["url"] !== "string") {
        return;
    }
    const url = data["url"];
    const clients = await scope.clients.matchAll({
        type: "window",
        includeUncontrolled: true,
    });
    const target_origin = new URL(url).origin;
    for (const client of clients) {
        if (new URL(client.url).origin === target_origin) {
            try {
                await client.focus();
                await client.navigate(url);
                return;
            } catch {
                // navigate() rejects for a client this worker does not control
                // yet; fall through to opening a new window.
                break;
            }
        }
    }
    await scope.clients.openWindow(url);
}

export type {MessagePayload};
