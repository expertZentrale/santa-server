from django.test.runner import DiscoverRunner
from django.test.utils import override_settings

from santa_server import settings_common

# the sign-in and the name of the server as they come, not as .devcontainer/.env of a developer sets them
DEFAULT_SETTINGS = {name: getattr(settings_common, name) for name in dir(settings_common)
                    if name.startswith(("OIDC_", "SANTA_"))}


class EnglishTestRunner(DiscoverRunner):
    """The tests check the English texts; the default language of the server (LANGUAGE_CODE) may be German.

    The e-mails are on and sent at once, not in a thread, so the tests find them in mail.outbox (Django's test
    backend, no mail server). The OIDC_* and SANTA_* settings are the defaults, whatever the environment
    (.devcontainer/.env) sets.
    """

    def setup_test_environment(self, **kwargs):
        super().setup_test_environment(**kwargs)
        self._english = override_settings(LANGUAGE_CODE="en", EMAIL_HOST="localhost", EMAIL_NOTIFICATIONS_ENABLED=True,
                                          EMAIL_SEND_IN_BACKGROUND=False, **DEFAULT_SETTINGS)
        self._english.enable()

    def teardown_test_environment(self, **kwargs):
        self._english.disable()
        super().teardown_test_environment(**kwargs)
