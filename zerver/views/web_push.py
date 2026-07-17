from typing import Annotated

from django.http import HttpRequest, HttpResponse
from pydantic import AfterValidator, StringConstraints

from zerver.actions.web_push import (
    do_register_web_push_subscription,
    do_remove_web_push_subscription,
)
from zerver.decorator import human_users_only
from zerver.lib.response import json_success
from zerver.lib.typed_endpoint import typed_endpoint
from zerver.lib.typed_endpoint_validators import (
    check_https_url,
    check_web_push_auth_secret,
    check_web_push_p256dh_key,
)
from zerver.models.users import UserProfile


@human_users_only
@typed_endpoint
def add_web_push_subscription(
    request: HttpRequest,
    user_profile: UserProfile,
    *,
    endpoint: Annotated[str, StringConstraints(max_length=2000), AfterValidator(check_https_url)],
    p256dh_key: Annotated[
        str, StringConstraints(max_length=255), AfterValidator(check_web_push_p256dh_key)
    ],
    auth_secret: Annotated[
        str, StringConstraints(max_length=255), AfterValidator(check_web_push_auth_secret)
    ],
    user_agent: Annotated[str, StringConstraints(max_length=255)] = "",
) -> HttpResponse:
    do_register_web_push_subscription(
        user_profile,
        endpoint=endpoint,
        p256dh_key=p256dh_key,
        auth_secret=auth_secret,
        user_agent=user_agent,
    )
    return json_success(request)


@human_users_only
@typed_endpoint
def remove_web_push_subscription(
    request: HttpRequest,
    user_profile: UserProfile,
    *,
    endpoint: Annotated[str, StringConstraints(max_length=2000)],
) -> HttpResponse:
    do_remove_web_push_subscription(user_profile, endpoint=endpoint)
    return json_success(request)
