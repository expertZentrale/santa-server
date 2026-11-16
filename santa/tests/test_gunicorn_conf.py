import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from santa.tests.test_settings_env import REQUIRED
from santa_server import settings_common

CONF_FILE = Path(__file__).resolve().parents[2] / "gunicorn.conf.py"


def load_conf(**values):
    """Import gunicorn.conf.py (the gunicorn settings of the image) with these Django settings, as settings_common.py
    has them otherwise: the values of a test environment don't count"""
    defaults = {name: getattr(settings_common, name) for name in GUNICORN_SETTINGS}
    spec = importlib.util.spec_from_file_location("gunicorn_conf_under_test", CONF_FILE)
    module = importlib.util.module_from_spec(spec)
    with override_settings(**{**defaults, **values}):
        spec.loader.exec_module(module)
    return module


GUNICORN_SETTINGS = ("GUNICORN_WORKERS", "GUNICORN_THREADS", "GUNICORN_WORKER_CLASS", "GUNICORN_TIMEOUT",
                     "GUNICORN_MAX_REQUESTS", "GUNICORN_ACCESS_LOG", "LOG_LEVEL")


class GunicornConfTestCase(SimpleTestCase):
    def test_defaults(self):
        conf = load_conf()
        self.assertEqual((conf.worker_class, conf.workers, conf.threads, conf.timeout),
                         ("gthread", 2, 4, 120))
        self.assertEqual((conf.max_requests, conf.max_requests_jitter), (1000, 100))
        self.assertEqual(conf.bind, "0.0.0.0:8000")
        self.assertTrue(conf.control_socket_disable)

    def test_the_values_come_from_the_settings(self):
        # set as variables or in the settings module of the deployment, whatever its name: both end up here
        conf = load_conf(GUNICORN_WORKERS=1, GUNICORN_THREADS=8, GUNICORN_TIMEOUT=300, GUNICORN_MAX_REQUESTS=0)
        self.assertEqual((conf.workers, conf.threads, conf.timeout), (1, 8, 300))
        # 0 turns the recycling off, without jitter
        self.assertEqual((conf.max_requests, conf.max_requests_jitter), (0, 0))

    def test_sync_worker_gets_one_thread(self):
        # gunicorn would switch to gthread with more than one thread
        self.assertEqual(load_conf(GUNICORN_WORKER_CLASS="sync").threads, 1)
        self.assertEqual(load_conf(GUNICORN_WORKER_CLASS="sync", GUNICORN_THREADS=3).threads, 3)

    def test_invalid_values_stop_the_start(self):
        # what settings.py doesn't check: values a settings module sets itself
        for name, value in [("GUNICORN_WORKERS", "2"), ("GUNICORN_WORKERS", 0), ("GUNICORN_THREADS", -1),
                            ("GUNICORN_MAX_REQUESTS", -5), ("GUNICORN_TIMEOUT", True), ("LOG_LEVEL", "loud")]:
            with self.subTest(name=name, value=value), self.assertRaises(SystemExit) as raised:
                load_conf(**{name: value})
            self.assertIn(name, str(raised.exception.code))

    def test_logging(self):
        conf = load_conf()
        self.assertEqual((conf.accesslog, conf.loglevel), ("-", "info"))
        conf = load_conf(GUNICORN_ACCESS_LOG=False, LOG_LEVEL="WARNING")
        self.assertEqual((conf.accesslog, conf.loglevel), (None, "warning"))

    def test_the_settings_module_of_the_deployment(self):
        """A settings module under any name that sets the variables before it imports settings.py: gunicorn gets
        them (before, gunicorn.conf.py read only the environment of the process)"""
        directory = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, directory, True)
        Path(directory, "deploy_conf_under_test.py").write_text(
            "import os\n"
            "os.environ['GUNICORN_ACCESS_LOG'] = 'false'\n"
            "os.environ['GUNICORN_WORKERS'] = '3'\n"
            "os.environ['LOG_LEVEL'] = 'ERROR'\n"
            "from santa_server.settings import *\n")
        script = ("import importlib.util; spec = importlib.util.spec_from_file_location('c', %r); "
                  "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); "
                  "print(m.accesslog, m.workers, m.loglevel)" % str(CONF_FILE))
        environ = {**os.environ, **REQUIRED, "DJANGO_SETTINGS_MODULE": "deploy_conf_under_test",
                   "PYTHONPATH": os.pathsep.join([directory, str(CONF_FILE.parent)])}
        for name in GUNICORN_SETTINGS:
            environ.pop(name, None)
        result = subprocess.run([sys.executable, "-c", script], env=environ, capture_output=True, text=True,
                                cwd=CONF_FILE.parent)
        self.assertEqual(result.stdout.split(), ["None", "3", "error"], result.stderr)

    def test_overridden_after_the_import(self):
        """A settings module that sets the values after `from santa_server.settings import *`: gunicorn and Django's
        own logs get the level; METRICS_ENABLED came too late for the apps, gunicorn follows them and a check warns"""
        directory = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, directory, True)
        Path(directory, "deploy_after_under_test.py").write_text(
            "from santa_server.settings import *\n"
            "LOG_LEVEL = 'ERROR'\n"
            "GUNICORN_ACCESS_LOG = False\n"
            "METRICS_ENABLED = not METRICS_ENABLED\n")
        script = ("import importlib.util, logging; spec = importlib.util.spec_from_file_location('c', %r); "
                  "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); "
                  "import django; django.setup(); from django.conf import settings; from django.core import checks; "
                  "print(m.accesslog, m.loglevel, logging.getLevelName(logging.getLogger().level), "
                  "m.metrics_enabled() == ('django_prometheus' in settings.INSTALLED_APPS "
                  "and settings.METRICS_ENABLED), "
                  "[message.id for message in checks.run_checks() if message.id.startswith('santa.')])"
                  % str(CONF_FILE))
        environ = {**os.environ, **REQUIRED, "DJANGO_SETTINGS_MODULE": "deploy_after_under_test",
                   "PYTHONPATH": os.pathsep.join([directory, str(CONF_FILE.parent)])}
        for name in (*GUNICORN_SETTINGS, "METRICS_ENABLED"):
            environ.pop(name, None)
        result = subprocess.run([sys.executable, "-c", script], env=environ, capture_output=True, text=True,
                                cwd=CONF_FILE.parent)
        self.assertEqual(result.stdout.split(), ["None", "error", "ERROR", "True", "['santa.W001']"], result.stderr)

    def access_lines(self, path, status):
        from gunicorn.config import Config
        config = Config()
        config.set("accesslog", "-")
        logger = load_conf().SyncTokenRedactingLogger(config)
        environ = {"REQUEST_METHOD": "GET", "RAW_URI": path, "PATH_INFO": path, "SERVER_PROTOCOL": "HTTP/1.1"}
        response = SimpleNamespace(status=status, sent=2, headers=[])
        with patch.object(logger.access_log, "info") as info:
            logger.access(response, SimpleNamespace(headers=[]), environ, SimpleNamespace(seconds=0, microseconds=1))
        return info.call_count

    def test_the_probes_answering_fine_are_not_in_the_access_log(self):
        for path in ("/health", "/ready", "/metrics", "/metrics/"):
            with self.subTest(path=path):
                self.assertEqual(self.access_lines(path, "200 OK"), 0)
        # a failing probe and every other request still are
        self.assertEqual(self.access_lines("/ready", "503 Service Unavailable"), 1)
        self.assertEqual(self.access_lines("/console/", "200 OK"), 1)
        self.assertEqual(self.access_lines("/healthy", "200 OK"), 1)


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
        conf = load_conf()
        with patch.dict("os.environ", {}, clear=True), patch.object(conf, "METRICS_DIR", directory), \
                override_settings(METRICS_ENABLED=True):
            conf.on_starting(None)
            self.assertEqual(os.environ["PROMETHEUS_MULTIPROC_DIR"], directory)
        self.assertEqual(os.listdir(directory), [])

    def test_no_directory_without_metrics(self):
        conf = load_conf()
        with patch.dict("os.environ", {}, clear=True), override_settings(METRICS_ENABLED=False):
            conf.on_starting(None)
            self.assertNotIn("PROMETHEUS_MULTIPROC_DIR", os.environ)

    def test_no_directory_when_the_apps_have_no_metrics(self):
        # METRICS_ENABLED set after settings.py left django_prometheus out: the workers have no metrics
        conf = load_conf()
        values = {"METRICS_ENABLED": True, "INSTALLED_APPS": ["santa"]}
        with patch.object(conf, "setting", values.get):
            self.assertFalse(conf.metrics_enabled())

    def run_worker(self, count):
        environ = {**os.environ, "PROMETHEUS_MULTIPROC_DIR": self.directory}
        subprocess.run([sys.executable, "-c", WORKER.format(count=count)], env=environ, check=True)

    @override_settings(METRICS_ENABLED=True)
    def test_the_export_sums_the_workers_also_after_they_ended(self):
        self.run_worker(2)
        self.run_worker(3)
        conf = load_conf()
        with patch.dict("os.environ", {"PROMETHEUS_MULTIPROC_DIR": self.directory}):
            # a worker that ended: its counters stay in the sum
            conf.child_exit(None, SimpleNamespace(pid=os.getpid()))
            response = self.client.get("/metrics")
        self.assertEqual(response.status_code, 200)
        self.assertIn("santa_test_worker_requests_total 5.0", response.content.decode())
