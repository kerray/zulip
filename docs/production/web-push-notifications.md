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

Web push is off by default. To enable it, add to `/etc/zulip/settings.py`:

```python
WEB_PUSH_ENABLED = True
```

and restart the server with `/home/zulip/deployments/current/scripts/restart-server`.

There is no key material to manage. When you install or upgrade Zulip,
`generate-secrets` creates a VAPID keypair and stores it as the
`web_push_vapid_private_key` secret in `/etc/zulip/zulip-secrets.conf`; the
browser-facing public key is derived from it at server start, so the two halves
can never drift apart. Enabling the setting is all that is required.

```{important}
Back up `/etc/zulip/zulip-secrets.conf` before enabling web push, and keep it
backed up. Browsers subscribe against the public half of this keypair, so
losing or replacing the private key invalidates every existing subscription:
each browser must re-subscribe before it receives notifications again, which
an already-subscribed browser does automatically the next time it loads Zulip.
```

A user enables web push under **Personal settings → Notifications** with the
**Enable web push notifications** setting. It is off by default, so each user
opts in. The setting is account-wide, but a browser only receives notifications
once it also holds a push subscription:

- Toggling the setting on prompts the browser the user is on for notification
  permission and subscribes it; toggling it off unsubscribes it.
- To receive notifications in an **additional browser or device**, the user
  opens Zulip there and clicks the **Enable push notifications in this
  browser** button that appears in the notification settings, which requests
  permission and subscribes that browser. Every browser is enrolled
  explicitly; turning the setting on in one browser never subscribes another.

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

Web push payloads are encrypted end-to-end between your Zulip server and the
user's browser using standard Web Push payload encryption (RFC 8291): the
browser generates the encryption keypair, and only it can decrypt the payload.
The push services that relay these messages (operated by Google, Mozilla, Apple,
and other browser vendors) see only ciphertext, never the notification's
contents.

The difference from Zulip's [end-to-end encrypted mobile push
notifications](mobile-push-notifications.md) is one of trust model rather than
content confidentiality: the browser itself decrypts the payload, so the browser
vendor is part of the trust chain, and the push service can observe metadata
such as the timing, frequency, and size of your notifications (but not their
contents).

As with mobile notifications, Zulip includes only a short plain-text summary in
the payload, and honors your **"Include content of messages in desktop
notifications"** preference: when it is disabled, direct message notifications
omit the message content.

## Revoking a subscription

Because a subscription belongs to a browser rather than to a login session, it
outlives that session unless something removes it. Regenerating a user's API key
deletes all of their web push subscriptions, alongside their mobile push
registrations, so `manage.py logout_all_users --rotate-api-keys` revokes every
notification channel a user has. Browsers the user still uses re-register on
their next page load.

```{note}
Logging out of the web app does not by itself unsubscribe that browser. A user
who has been receiving notifications on a shared computer should turn **Enable
web push notifications** off there before logging out. Failing that, the
subscription stops serving them as soon as anyone next logs in to Zulip in that
browser, which either discards it or transfers it to the new user.
```
