from django.test.runner import DiscoverRunner
from django.test.utils import override_settings


class EnglishTestRunner(DiscoverRunner):
    """The tests check the English texts; the default language of the server (LANGUAGE_CODE) may be German.

    The e-mails are sent at once, not in a thread, so the tests find them in mail.outbox.
    """

    def setup_test_environment(self, **kwargs):
        super().setup_test_environment(**kwargs)
        self._english = override_settings(LANGUAGE_CODE="en", EMAIL_SEND_IN_BACKGROUND=False)
        self._english.enable()

    def teardown_test_environment(self, **kwargs):
        self._english.disable()
        super().teardown_test_environment(**kwargs)
