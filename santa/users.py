"""The signed-in users: their profile (preferences) and their picture."""
import hashlib
import zoneinfo
from functools import cache
from urllib.parse import urlencode

from .models import UserProfile


def profile_for(user):
    """The profile of the user, created with the defaults on first use (cached on the user)"""
    if not hasattr(user, "_santa_profile"):
        user._santa_profile, _ = UserProfile.objects.get_or_create(user=user)
    return user._santa_profile


def gravatar_url(user, size=64):
    # Gravatar takes the SHA-256 of the trimmed, lower case e-mail address. No address: the default picture.
    email = (user.email or user.get_username() or "").strip().lower()
    digest = hashlib.sha256(email.encode()).hexdigest()
    return f"https://gravatar.com/avatar/{digest}?{urlencode({'s': size * 2, 'd': 'mp'})}"


# the browser sends its time zone in this cookie (console.js)
TIME_ZONE_COOKIE = "santa_tz"


@cache
def time_zone_names():
    return frozenset(zoneinfo.available_timezones())


def time_zone_for(request):
    """The time zone of the profile, else the one of the browser; None: TIME_ZONE of the server"""
    user = getattr(request, "user", None)
    name = profile_for(user).time_zone if user is not None and user.is_authenticated else ""
    name = name or request.COOKIES.get(TIME_ZONE_COOKIE, "")
    return zoneinfo.ZoneInfo(name) if name in time_zone_names() else None
