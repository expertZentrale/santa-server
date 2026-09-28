from django.db import connection
from django.http import HttpResponse
from django.utils import translation

from .users import profile_for


class HealthCheckMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path == "/health":
            return HttpResponse("ok", content_type="text/plain")
        if request.path == "/ready":
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT 1")
            except Exception:
                return HttpResponse("database unavailable", status=503, content_type="text/plain")
            return HttpResponse("ok", content_type="text/plain")
        return self.get_response(request)


class UserLanguageMiddleware:
    """The language chosen in the profile wins over the one of the browser (LocaleMiddleware)"""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            language = profile_for(user).language
            if language:
                translation.activate(language)
                request.LANGUAGE_CODE = language
        return self.get_response(request)
