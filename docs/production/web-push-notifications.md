# Web push notifications

Zulip's web app can deliver browser notifications for new messages using [Web
Push](https://developer.mozilla.org/en-US/docs/Web/API/Push_API) (RFC 8030 and
8291), including to a Zulip [progressive web app](https://web.dev/explore/progressive-web-apps)
installed to a device's home screen. Users see these notifications even when the
Zulip tab is in the background or closed, as long as the browser is running.

Unlike [mobile push notifications](mobile-push-notifications.md), web push needs
no push notification service (bouncer): your server talks directly to each
browser's push service, authenticating its requests with a
[VAPID](https://datatracker.ietf.org/doc/html/rfc8292) keypair that is unique to
your server.

## Setup

There is nothing to configure. When you install or upgrade Zulip,
`generate-secrets` creates a VAPID keypair and stores it as the
`web_push_vapid_private_key` secret in `/etc/zulip/zulip-secrets.conf`. The
browser-facing public key is derived from it at server start, so the two halves
can never drift apart. Once the secret is present, the web app offers web push
notifications automatically.

A user enables web push under **Personal settings → Notifications** with the
**Web push notifications** setting. Toggling it on prompts the browser for
notification permission and subscribes that browser; toggling it off
unsubscribes it. Because the browser permission prompt is the real opt-in, the
server-side setting defaults to on.

## Requirements

- **HTTPS is required.** Service workers and the Push API are only available on
  secure origins, so your server must be served over HTTPS (as a production
  Zulip server always is).
- **No outbound push service allowlisting is needed** beyond normal outbound
  HTTPS: your server sends encrypted notifications directly to the push
  endpoints operated by each browser vendor (for example, Google, Mozilla, and
  Apple).
- **The `sub` claim** in the VAPID request identifies your server to the push
  services as a `mailto:` contact. It defaults to your server's
  `ZULIP_ADMINISTRATOR`; you can override it by setting
  `WEB_PUSH_VAPID_CONTACT_EMAIL` in `/etc/zulip/settings.py`.

## Browser support

Web push is supported by current versions of Chrome, Edge, Firefox, and other
Chromium-based browsers on desktop and Android.

On **iOS and iPadOS**, web push is only available when the Zulip web app is
**installed to the home screen** ("Add to Home Screen") and launched from there,
and requires iOS/iPadOS 16.4 or later. It does not work in a regular Safari
browser tab. Because of this, desktop browsers and Android are the primary
targets for web push; iOS is best-effort for users who install the PWA.

## Privacy

Web push payloads are encrypted in transit to the browser's push service, but —
unlike Zulip's end-to-end encrypted mobile push notifications — they are not
end-to-end encrypted to the user's device, and their contents are visible to the
browser. Zulip only includes the same short plain-text summary used for mobile
notifications, and honors your **"Include content of messages in desktop
notifications"** preference: when it is disabled, direct message notifications
omit the message content.
