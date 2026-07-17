import * as blueslip from "./blueslip.ts";
import * as channel from "./channel.ts";
import {realm} from "./state_data.ts";
import {user_settings} from "./user_settings.ts";

// Registers a service worker and manages the browser's Web Push subscription,
// posting it to the server's web_push_subscriptions endpoint. Requesting the
// Notification permission requires a user gesture, so a browser normally
// subscribes from the settings toggle (handle_setting_change). initialize()
// subscribes on load only when the setting is on AND this browser has already
// granted the Notification permission (no prompt needed) — otherwise it merely
// re-posts a subscription this browser already has, to keep the server's keys
// fresh. If the setting is off but a stale subscription lingers (e.g. from a
// previous account holder in this browser), initialize() destroys it.

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

// All operations that MUTATE this browser's push subscription — the load-time
// sync (sync_on_load) and the settings-toggle subscribe/unsubscribe — run
// through this single serialized promise chain, so they never interleave across
// their awaits. Without it, a load-time auto-subscribe could race a concurrent
// user disable: the disable observes no subscription (the load path has not
// created it yet) and does nothing, then the load path creates one, leaving the
// setting off with a live subscription. Serializing removes that window; each
// queued operation additionally re-reads user_settings and the live
// subscription state when it actually runs (not when it was enqueued), so it
// acts on the current desired state rather than one captured earlier. The
// settings-toggle subscribe additionally guards on a desired-state token (see
// desired_generation) rather than re-reading the setting, which the enable flow
// persists only after subscribe succeeds.
//
// Only these top-level entry points enqueue; the helpers they call (subscribe,
// unsubscribe, post_subscription, …) must NOT self-enqueue, or an op that calls
// another from inside the chain would deadlock waiting on itself.
let pending: Promise<void> = Promise.resolve();

// Monotonic token identifying the LATEST desired-state intent. Every call to
// handle_setting_change (enable or disable) bumps it and captures its own value
// BEFORE awaiting anything; a queued subscribe/unsubscribe then aborts as an
// (effective) no-op when a newer intent has bumped the token past the value it
// captured. This is what makes a delayed permission grant safe: a user can
// toggle ON (opening a slow permission prompt), then toggle OFF (which bumps the
// token and completes), then finally grant the still-open prompt — the ON path's
// queued subscribe sees the token has moved on and does nothing, instead of
// resurrecting the subscription the user just disabled. It also settles a rapid
// ON→OFF→ON: only the last toggle's op takes effect. We deliberately CANNOT
// instead re-read user_settings inside the queued subscribe, because the enable
// flow persists the setting only AFTER subscribe succeeds, so a queued subscribe
// would always read the setting as still-off and wrongly abort itself; the token
// avoids that trap. The load-time sync (sync_on_load) captures the current token
// at enqueue WITHOUT bumping it — it carries no new intent of its own — so any
// user toggle enqueued after it supersedes it.
let desired_generation = 0;

// This is deliberately promise-chain plumbing: it forks a failure-swallowing
// tail from the caller-visible promise WITHOUT awaiting, so it cannot be
// rewritten with async/await (awaiting here would serialize the fork away and
// change the queue semantics). The promise lint rules are therefore disabled
// for this function on purpose.
// eslint-disable-next-line @typescript-eslint/promise-function-async
function enqueue<T>(op: () => Promise<T>): Promise<T> {
    // eslint-disable-next-line promise/prefer-await-to-then
    const result = pending.then(op);
    // Advance the chain regardless of this op's outcome, swallowing rejections
    // on the shared tail so one failed op neither breaks the chain nor surfaces
    // as an unhandled rejection. Callers still observe rejection via `result`.
    // eslint-disable-next-line promise/prefer-await-to-then
    pending = result.then(
        () => undefined,
        () => undefined,
    );
    return result;
}

function array_buffer_to_base64url(buffer: ArrayBuffer): string {
    let binary = "";
    for (const byte of new Uint8Array(buffer)) {
        binary += String.fromCodePoint(byte);
    }
    return btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replaceAll("=", "");
}

function base64url_to_uint8_array(base64url: string): Uint8Array<ArrayBuffer> {
    const padding = "=".repeat((4 - (base64url.length % 4)) % 4);
    const base64 = (base64url + padding).replaceAll("-", "+").replaceAll("_", "/");
    const binary = atob(base64);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i += 1) {
        const code = binary.codePointAt(i);
        if (code !== undefined) {
            bytes[i] = code;
        }
    }
    return bytes;
}

function array_buffers_equal(a: ArrayBuffer, b: Uint8Array): boolean {
    const a_bytes = new Uint8Array(a);
    if (a_bytes.length !== b.length) {
        return false;
    }
    for (const [index, byte] of a_bytes.entries()) {
        if (byte !== b[index]) {
            return false;
        }
    }
    return true;
}

function subscription_uses_key(subscription: PushSubscription, server_key: Uint8Array): boolean {
    const application_server_key = subscription.options.applicationServerKey;
    if (application_server_key === null) {
        return false;
    }
    return array_buffers_equal(application_server_key, server_key);
}

// Registers the subscription with the server. Resolves once the server has
// stored it (so callers can report success only after it is durably
// registered) and rejects if the request fails.
async function post_subscription(subscription: PushSubscription): Promise<void> {
    const p256dh_key = subscription.getKey("p256dh");
    const auth_secret = subscription.getKey("auth");
    if (p256dh_key === null || auth_secret === null) {
        return;
    }
    await new Promise<void>((resolve, reject) => {
        channel.post({
            url: SUBSCRIPTIONS_URL,
            data: {
                endpoint: subscription.endpoint,
                p256dh_key: array_buffer_to_base64url(p256dh_key),
                auth_secret: array_buffer_to_base64url(auth_secret),
                // The server caps user_agent at 255 characters (and rejects
                // longer values rather than truncating), so trim it here.
                user_agent: navigator.userAgent.slice(0, 255),
            },
            success() {
                resolve();
            },
            error() {
                reject(new Error("Failed to register web push subscription"));
            },
        });
    });
}

async function subscribe(): Promise<void> {
    const registration = await navigator.serviceWorker.ready;
    const server_key = base64url_to_uint8_array(realm.server_web_push_vapid_public_key);
    const existing = await registration.pushManager.getSubscription();
    // A VAPID key rotation leaves an existing subscription bound to the old
    // key, which the server can no longer send to. Drop it so we re-subscribe
    // with the current key rather than stranding this browser.
    if (existing !== null && !subscription_uses_key(existing, server_key)) {
        await existing.unsubscribe();
    }
    const subscription = await registration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: server_key,
    });
    await post_subscription(subscription);
}

// Deletes the server-side row for an endpoint. Resolves once the server has
// removed it and rejects if the request fails.
async function delete_subscription_on_server(endpoint: string): Promise<void> {
    await new Promise<void>((resolve, reject) => {
        channel.del({
            url: SUBSCRIPTIONS_URL,
            data: {endpoint},
            success() {
                resolve();
            },
            error() {
                reject(new Error("Failed to unregister web push subscription"));
            },
        });
    });
}

async function unsubscribe(): Promise<void> {
    const registration = await navigator.serviceWorker.getRegistration("/");
    const subscription = await registration?.pushManager.getSubscription();
    if (subscription === undefined || subscription === null) {
        return;
    }
    // Delete the server row FIRST. If that fails we reject, so the caller keeps
    // the toggle checked and this browser stays subscribed — the two states
    // remain consistent. Only once the row is gone do we unsubscribe the
    // browser; a failure there is benign (no row means no sends), so we log it
    // rather than reverting a change the server has already accepted.
    await delete_subscription_on_server(subscription.endpoint);
    try {
        await subscription.unsubscribe();
    } catch (error) {
        blueslip.warn("Failed to unsubscribe browser from web push", {error});
    }
}

// Runs on page load. It may re-post or (when the setting is on and permission
// was already granted) create this browser's subscription, but it never
// triggers a permission prompt: pushManager.subscribe() only prompts when the
// permission is "default", and we gate creation on "granted". A new
// subscription that would need a prompt only ever happens from
// handle_setting_change, inside a user gesture.
async function sync_on_load(my_generation: number): Promise<void> {
    const registration = await navigator.serviceWorker.ready;
    if (my_generation !== desired_generation) {
        // A settings toggle bumped the desired-state token after this load-sync
        // was enqueued: its queued subscribe/unsubscribe fully expresses the
        // current desired state, so this stale load-sync must not act.
        return;
    }
    // Read the setting and the live subscription state HERE — when this queued
    // op actually runs, not when it was enqueued at page load. A user toggle
    // may have run (and been serialized) ahead of us; re-reading makes us take
    // the branch matching the current state and abort cleanly if a subscription
    // appeared or disappeared meanwhile.
    const subscription = await registration.pushManager.getSubscription();
    if (user_settings.enable_web_push_notifications) {
        if (subscription !== null) {
            // Re-post the existing subscription to refresh the server's keys
            // and (if this browser changed hands) transfer ownership to me.
            await post_subscription(subscription);
        } else if (Notification.permission === "granted") {
            // The user opted in globally and this browser has already granted
            // permission, so enroll it without a prompt. This is legitimate:
            // the permission is already "granted", so subscribe() cannot prompt.
            await subscribe();
        }
        return;
    }
    // The setting is off for the current user, but a subscription is still
    // present — typically because a previous account holder subscribed this
    // browser. Destroy it: unsubscribing invalidates the endpoint at the push
    // service, so the previous owner's sends 404/410 and the server prunes the
    // row. Also fire a best-effort server DELETE in case the row now belongs to
    // the current user (whose setting is off, so it should not exist).
    if (subscription !== null) {
        const endpoint = subscription.endpoint;
        await subscription.unsubscribe();
        await delete_subscription_on_server(endpoint).catch((error: unknown) => {
            blueslip.warn("Failed to delete stale web push subscription", {error});
        });
    }
}

export function initialize(): void {
    if (!is_configured()) {
        return;
    }
    void navigator.serviceWorker.register(SERVICE_WORKER_URL, {scope: "/"});
    // Capture the current intent token WITHOUT bumping it: a load-sync is not a
    // new intent, so any user toggle enqueued after it supersedes it.
    const my_generation = desired_generation;
    void enqueue(async () => {
        try {
            await sync_on_load(my_generation);
        } catch (error) {
            blueslip.warn("Failed to sync web push subscription", {error});
        }
    });
}

// Whether this server supports web push at all (VAPID key present and browser
// APIs available). The settings UI uses this to decide whether to surface the
// per-browser enrollment banner.
export function is_web_push_configured(): boolean {
    return is_configured();
}

// Whether this browser currently holds a push subscription. The settings UI
// uses this (with the setting and permission state) to decide whether this
// browser still needs to be enrolled.
export async function has_local_subscription(): Promise<boolean> {
    if (!is_configured()) {
        return false;
    }
    const registration = await navigator.serviceWorker.ready;
    const subscription = await registration.pushManager.getSubscription();
    return subscription !== null;
}

export type SettingChangeResult = "applied" | "permission-denied" | "superseded";

export async function handle_setting_change(enabled: boolean): Promise<SettingChangeResult> {
    // Invoked from the settings toggle change handler, so this runs inside a
    // user gesture and may request the Notification permission. Returns
    // "applied" when the change took effect in this browser,
    // "permission-denied" when enabling failed because the notification
    // permission was not granted (the caller reverts and explains), and
    // "superseded" when a later toggle overtook this one while it was waiting —
    // the caller must then neither persist the stale value nor show a banner;
    // the newer toggle's handler owns the outcome.
    if (!is_configured()) {
        return "applied";
    }
    // Record THIS toggle as the latest desired-state intent before any await, so
    // a later toggle can supersede it and make our queued op abort (see
    // desired_generation).
    desired_generation += 1;
    const my_generation = desired_generation;
    if (enabled) {
        // Request permission BEFORE entering the serialized queue: it must run
        // synchronously within the toggle's user gesture, and awaiting the
        // queue first could forfeit that gesture. Subscribing needs no gesture
        // once permission is granted, so it is safe to serialize.
        const permission = await Notification.requestPermission();
        if (my_generation !== desired_generation) {
            // A later toggle superseded this enable while the permission
            // prompt was open; report that rather than the permission outcome.
            return "superseded";
        }
        if (permission !== "granted") {
            return "permission-denied";
        }
        const acted = await enqueue(async () => {
            if (my_generation !== desired_generation) {
                // Superseded after permission was granted; do not resurrect
                // the subscription.
                return false;
            }
            await subscribe();
            return true;
        });
        return acted ? "applied" : "superseded";
    }
    const acted = await enqueue(async () => {
        if (my_generation !== desired_generation) {
            return false;
        }
        await unsubscribe();
        return true;
    });
    return acted ? "applied" : "superseded";
}
