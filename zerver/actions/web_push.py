from zerver.models.push_notifications import WebPushSubscription
from zerver.models.users import UserProfile


def do_register_web_push_subscription(
    user_profile: UserProfile,
    *,
    endpoint: str,
    p256dh_key: str,
    auth_secret: str,
    user_agent: str,
) -> WebPushSubscription:
    # The endpoint is the natural key of a browser subscription, so
    # re-subscribing from the same browser refreshes the keys rather than
    # creating a duplicate row.
    subscription, _created = WebPushSubscription.objects.update_or_create(
        user=user_profile,
        endpoint=endpoint,
        defaults=dict(
            p256dh_key=p256dh_key,
            auth_secret=auth_secret,
            user_agent=user_agent,
        ),
    )
    return subscription


def do_remove_web_push_subscription(user_profile: UserProfile, *, endpoint: str) -> None:
    WebPushSubscription.objects.filter(user=user_profile, endpoint=endpoint).delete()
