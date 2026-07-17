import os

from django.conf import settings
from django.http import HttpRequest, HttpResponse

# webpack emits the service worker as an unhashed bundle (see the
# output.filename override in web/webpack.config.ts) so it lives at a stable
# path.
SERVICE_WORKER_BUNDLE = os.path.join("webpack-bundles", "service-worker.js")


def get_service_worker_path() -> str | None:
    if settings.DEBUG:
        # In the development server webpack-dev-server serves bundles from
        # memory under /webpack/; use the staticfiles finders to locate the
        # on-disk copy when one exists (e.g. a production-static build).
        from django.contrib.staticfiles.finders import find

        return find(SERVICE_WORKER_BUNDLE)
    return os.path.join(settings.STATIC_ROOT, SERVICE_WORKER_BUNDLE)


def serve_service_worker(request: HttpRequest) -> HttpResponse:
    """Serve the web push service worker from the site root.

    A service worker can only control the pages under its own URL's directory,
    so it must be served from "/" (not "/static/...") for its scope to cover
    the whole app. Serving it through Django works in both development and
    production, where nginx proxies the unmatched "/service-worker.js" here;
    an nginx alias is an optional production optimization.
    """
    path = get_service_worker_path()
    if path is None or not os.path.exists(path):
        return HttpResponse(status=404)

    with open(path, "rb") as f:
        content = f.read()

    response = HttpResponse(content, content_type="text/javascript")
    # Allow the worker to claim the root scope even though its URL is "/".
    response["Service-Worker-Allowed"] = "/"
    # Service workers must be revalidated so updates roll out promptly.
    response["Cache-Control"] = "no-cache"
    return response
