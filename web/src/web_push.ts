import * as blueslip from "./blueslip.ts";
import * as channel from "./channel.ts";
import {realm} from "./state_data.ts";
import {user_settings} from "./user_settings.ts";

// Registers a service worker and manages the browser's Web Push subscription,
// posting it to the server's web_push_subscriptions endpoint. Requesting the
// Notification permission requires a user gesture, so that only happens from
// the settings toggle (handle_setting_change); initialize() just refreshes an
// existing subscription when permission was already granted.

const SERVICE_WORKER_URL = "/service-worker.js";
const SUBSCRIPTIONS_URL = "/json/users/me/web_push_subscriptions";

function is_supported(): boolean {
    return (
        typeof navigator !== "undefined" &&
        "serviceWorker" in navigator &&
        typeof window !== "undefined" &&
        "PushManager" in window &&
        "Notification" in window
    );
}

function is_configured(): boolean {
    return is_supported() && realm.server_web_push_vapid_public_key !== "";
}

function array_buffer_to_base64url(buffer: ArrayBuffer): string {
    let binary = "";
    for (const byte of new Uint8Array(buffer)) {
        binary += String.fromCodePoint(byte);
    }
    return btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replaceAll("=", "");
}

function base64url_to_uint8_array(base64url: string): Uint8Array {
    const padding = "=".repeat((4 - (base64url.length % 4)) % 4);
    const base64 = (base64url + padding).replaceAll("-", "+").replaceAll("_", "/");
    const binary = atob(base64);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i += 1) {
        bytes[i] = binary.codePointAt(i)!;
    }
    return bytes;
}

function post_subscription(subscription: PushSubscription): void {
    const p256dh_key = subscription.getKey("p256dh");
    const auth_secret = subscription.getKey("auth");
    if (p256dh_key === null || auth_secret === null) {
        return;
    }
    channel.post({
        url: SUBSCRIPTIONS_URL,
        data: {
            endpoint: subscription.endpoint,
            p256dh_key: array_buffer_to_base64url(p256dh_key),
            auth_secret: array_buffer_to_base64url(auth_secret),
            // The server caps user_agent at 255 characters (and rejects longer
            // values rather than truncating), so trim it here.
            user_agent: navigator.userAgent.slice(0, 255),
        },
    });
}

async function subscribe(): Promise<void> {
    const registration = await navigator.serviceWorker.ready;
    const subscription = await registration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: base64url_to_uint8_array(realm.server_web_push_vapid_public_key),
    });
    post_subscription(subscription);
}

async function unsubscribe(): Promise<void> {
    const registration = await navigator.serviceWorker.getRegistration("/");
    const subscription = await registration?.pushManager.getSubscription();
    if (subscription === undefined || subscription === null) {
        return;
    }
    const endpoint = subscription.endpoint;
    await subscription.unsubscribe();
    channel.del({url: SUBSCRIPTIONS_URL, data: {endpoint}});
}

export function initialize(): void {
    if (!is_configured()) {
        return;
    }
    void navigator.serviceWorker.register(SERVICE_WORKER_URL, {scope: "/"});
    // Silently refresh the subscription only when the user already opted in;
    // requesting a new permission needs a user gesture (see below).
    if (user_settings.enable_web_push_notifications && Notification.permission === "granted") {
        void subscribe().catch((error: unknown) => {
            blueslip.warn("Failed to subscribe to web push", {error});
        });
    }
}

export async function handle_setting_change(enabled: boolean): Promise<boolean> {
    // Invoked from the settings toggle change handler, so this runs inside a
    // user gesture and may request the Notification permission. Returns
    // whether the change can take effect in this browser — false only when
    // enabling failed because the notification permission was not granted,
    // so the caller can avoid persisting a setting that cannot work.
    if (!is_configured()) {
        return true;
    }
    if (enabled) {
        const permission = await Notification.requestPermission();
        if (permission !== "granted") {
            return false;
        }
        await subscribe();
        return true;
    }
    await unsubscribe();
    return true;
}
