import importlib.util
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase

CONF_FILE = Path(__file__).resolve().parents[2] / "gunicorn.conf.py"


def load_conf(environ):
    """Import gunicorn.conf.py (the gunicorn settings of the image) with these environment variables only"""
    spec = importlib.util.spec_from_file_location("gunicorn_conf_under_test", CONF_FILE)
    module = importlib.util.module_from_spec(spec)
    with patch.dict("os.environ", environ, clear=True):
        spec.loader.exec_module(module)
    return module


class GunicornConfTestCase(SimpleTestCase):
    def test_defaults(self):
        conf = load_conf({})
        self.assertEqual((conf.worker_class, conf.workers, conf.threads, conf.timeout),
                         ("gthread", 2, 4, 120))
        self.assertEqual((conf.max_requests, conf.max_requests_jitter), (1000, 100))
        self.assertEqual(conf.bind, "0.0.0.0:8000")
        self.assertTrue(conf.control_socket_disable)

    def test_overrides(self):
        conf = load_conf({"GUNICORN_WORKERS": "1", "GUNICORN_THREADS": "8", "GUNICORN_TIMEOUT": "300",
                          "GUNICORN_MAX_REQUESTS": "0"})
        self.assertEqual((conf.workers, conf.threads, conf.timeout), (1, 8, 300))
        # 0 turns the recycling off, without jitter
        self.assertEqual((conf.max_requests, conf.max_requests_jitter), (0, 0))

    def test_sync_worker_gets_one_thread(self):
        # gunicorn would switch to gthread with more than one thread
        self.assertEqual(load_conf({"GUNICORN_WORKER_CLASS": "sync"}).threads, 1)
        self.assertEqual(load_conf({"GUNICORN_WORKER_CLASS": "sync", "GUNICORN_THREADS": "3"}).threads, 3)

    def test_invalid_values_stop_the_start(self):
        for name, value in [("GUNICORN_WORKERS", "abc"), ("GUNICORN_WORKERS", "0"), ("GUNICORN_THREADS", "-1"),
                            ("GUNICORN_MAX_REQUESTS", "-5")]:
            with self.subTest(name=name, value=value), self.assertRaises(SystemExit) as raised:
                load_conf({name: value})
            self.assertIn(name, str(raised.exception.code))
