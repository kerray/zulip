/// <reference lib="webworker" />

// This file is bundled as its own webpack entry and served from the site root
// as /service-worker.js (see zerver/views/service_worker.py) so its scope can
// cover the whole app. It is deliberately tiny and imports no app code, so its
// bundle rarely changes and the browser's update checks stay cheap.
//
// It renders the OS notification from the compact JSON payload built by
// get_message_payload_webpush in zerver/lib/push_notifications.py.

declare const self: ServiceWorkerGlobalScope;

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

function parse_message_payload(data: unknown): MessagePayload | undefined {
    if (!is_record(data) || data.type !== "message") {
        return undefined;
    }
    return data as MessagePayload;
}

self.addEventListener("install", () => {
    void self.skipWaiting();
});

self.addEventListener("activate", (event) => {
    event.waitUntil(self.clients.claim());
});

self.addEventListener("push", (event) => {
    let data: unknown;
    try {
        data = event.data?.json();
    } catch {
        // A malformed payload; nothing safe to render. We only ever send
        // user-visible "message" payloads, so with the userVisibleOnly:true
        // subscription there is no silent-push case to worry about here.
        return;
    }
    const payload = parse_message_payload(data);
    if (payload === undefined) {
        return;
    }
    event.waitUntil(
        self.registration.showNotification(payload.title, {
            body: payload.body,
            icon: payload.icon,
            badge: payload.badge,
            tag: payload.tag,
            data: {url: payload.url},
        }),
    );
});

self.addEventListener("notificationclick", (event) => {
    event.notification.close();
    const data: unknown = event.notification.data;
    if (!is_record(data) || typeof data.url !== "string") {
        return;
    }
    const url = data.url;
    event.waitUntil(
        (async () => {
            const clients = (await self.clients.matchAll({
                type: "window",
                includeUncontrolled: true,
            })) as readonly WindowClient[];
            const target_origin = new URL(url).origin;
            for (const client of clients) {
                if (new URL(client.url).origin === target_origin) {
                    try {
                        await client.focus();
                        await client.navigate(url);
                        return;
                    } catch {
                        // navigate() rejects for a client this worker does not
                        // control yet; fall through to opening a new window.
                        break;
                    }
                }
            }
            await self.clients.openWindow(url);
        })(),
    );
});

export type {MessagePayload};
