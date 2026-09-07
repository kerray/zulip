from django.db import transaction

from zerver.models.push_notifications import WebPushSubscription
from zerver.models.users import UserProfile

# Cap the number of browser Web Push subscriptions retained per user. Each is a
# separate encrypted HTTPS POST at send time, and a browser that clears its
# storage re-subscribes under a fresh endpoint without deleting the old one, so
# without a cap a single user could accumulate unbounded dead endpoints. When
# the cap is exceeded we evict the least-recently-updated subscriptions rather
# than erroring, so re-subscribing after a storage reset always succeeds.
MAX_WEB_PUSH_SUBSCRIPTIONS_PER_USER = 10


def _evict_stale_subscriptions(user_profile: UserProfile, stale_ids: list[int]) -> None:
    # Re-filter on the current user rather than deleting by ID alone: a row
    # snapshotted as stale can be transferred to another user by a concurrent
    # registration between the snapshot and this delete (endpoints are globally
    # unique and re-subscribing transfers ownership), and an ID-only delete would
    # then destroy the new owner's live subscription. The user predicate makes
    # eviction a no-op for any row no longer ours.
    if stale_ids:
        WebPushSubscription.objects.filter(id__in=stale_ids, user=user_profile).delete()


def do_register_web_push_subscription(
    user_profile: UserProfile,
    *,
    endpoint: str,
    p256dh_key: str,
    auth_secret: str,
    user_agent: str,
) -> WebPushSubscription:
    # The endpoint is the globally-unique natural key of a browser
    # subscription. Keying update_or_create on the endpoint alone (not on
    # (user, endpoint)) means re-subscribing the same browser transfers
    # ownership to the current user — refreshing the keys and user — rather
    # than leaving a stale row still owned by whoever used the browser before,
    # which would deliver this user's messages to their session.
    with transaction.atomic(savepoint=False):
        # Serialize concurrent registrations for this user by locking the user
        # row first. Without it, two simultaneous registrations can both upsert
        # and then each slice off the cap independently, leaving more than
        # MAX_WEB_PUSH_SUBSCRIPTIONS_PER_USER rows; the lock makes the
        # upsert+eviction below run one user-registration at a time.
        #
        # Lock acquisition order is always the user row first, then the endpoint
        # upsert. The only residual cross-user deadlock is two transactions each
        # transferring an endpoint the other currently owns; Postgres breaks it
        # by aborting one transaction, which surfaces to the client as a
        # retryable error. Transferring an endpoint between two users
        # concurrently is rare enough that this comment, not retry machinery, is
        # the right treatment.
        UserProfile.objects.select_for_update(no_key=True).get(id=user_profile.id)

        subscription, _created = WebPushSubscription.objects.update_or_create(
            endpoint=endpoint,
            defaults=dict(
                user=user_profile,
                p256dh_key=p256dh_key,
                auth_secret=auth_secret,
                user_agent=user_agent,
            ),
        )

        # Enforce the per-user cap, evicting the oldest subscriptions by
        # last_updated. The row we just upserted has the newest last_updated
        # (auto_now), so it is always retained. The ownership-conditional delete
        # lives in _evict_stale_subscriptions so the eviction path is exercised
        # directly by tests rather than reimplemented by them.
        stale_ids = list(
            WebPushSubscription.objects.filter(user=user_profile)
            .order_by("-last_updated", "-id")
            .values_list("id", flat=True)[MAX_WEB_PUSH_SUBSCRIPTIONS_PER_USER:]
        )
        _evict_stale_subscriptions(user_profile, stale_ids)

    return subscription


def do_remove_web_push_subscription(user_profile: UserProfile, *, endpoint: str) -> None:
    WebPushSubscription.objects.filter(user=user_profile, endpoint=endpoint).delete()
