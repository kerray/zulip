/// <reference lib="webworker" />

// This file is bundled as its own webpack entry and served from the site root
// as /service-worker.js (see zerver/views/service_worker.py) so its scope can
// cover the whole app. It is deliberately tiny and imports no app code, so its
// bundle rarely changes and the browser's update checks stay cheap.
//
// It is only the event wiring; the behavior lives in
// service_worker_handlers.ts, which takes the scope as a parameter so it can
// be unit tested without a ServiceWorkerGlobalScope.

import {handle_notification_click, handle_push} from "./service_worker_handlers.ts";

declare const self: ServiceWorkerGlobalScope;

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
    event.waitUntil(handle_push(data, self));
});

self.addEventListener("notificationclick", (event) => {
    event.notification.close();
    event.waitUntil(handle_notification_click(event.notification.data, self));
});
