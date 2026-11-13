import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

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


WORKER = """
from prometheus_client import Counter
Counter("santa_test_worker_requests", "Requests of a worker").inc({count})
"""


class MultiprocessMetricsTestCase(SimpleTestCase):
    """Every gunicorn worker writes its metrics into one directory, /metrics sums them up"""

    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.directory, True)

    def test_on_starting_sets_and_empties_the_directory(self):
        directory = os.path.join(self.directory, "metrics")
        os.makedirs(directory)
        Path(directory, "counter_1.db").write_bytes(b"old")
        conf = load_conf({})
        with patch.dict("os.environ", {}, clear=True), patch.object(conf, "METRICS_DIR", directory):
            conf.on_starting(None)
            self.assertEqual(os.environ["PROMETHEUS_MULTIPROC_DIR"], directory)
        self.assertEqual(os.listdir(directory), [])

    def test_no_directory_without_metrics(self):
        conf = load_conf({})
        with patch.dict("os.environ", {"METRICS_ENABLED": "false"}, clear=True):
            conf.on_starting(None)
            self.assertNotIn("PROMETHEUS_MULTIPROC_DIR", os.environ)

    def run_worker(self, count):
        environ = {**os.environ, "PROMETHEUS_MULTIPROC_DIR": self.directory}
        subprocess.run([sys.executable, "-c", WORKER.format(count=count)], env=environ, check=True)

    @override_settings(METRICS_ENABLED=True)
    def test_the_export_sums_the_workers_also_after_they_ended(self):
        self.run_worker(2)
        self.run_worker(3)
        conf = load_conf({})
        with patch.dict("os.environ", {"PROMETHEUS_MULTIPROC_DIR": self.directory}):
            # a worker that ended: its counters stay in the sum
            conf.child_exit(None, SimpleNamespace(pid=os.getpid()))
            response = self.client.get("/metrics")
        self.assertEqual(response.status_code, 200)
        self.assertIn("santa_test_worker_requests_total 5.0", response.content.decode())
