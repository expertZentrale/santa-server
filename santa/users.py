"""The signed-in users: their profile (preferences) and their picture."""
import hashlib
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
