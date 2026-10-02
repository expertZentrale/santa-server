import os
import socket
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from django.test import SimpleTestCase

ENTRYPOINT = Path(__file__).resolve().parents[2] / "entrypoint.sh"


def free_port():
    """A port nothing listens on: the URL can't be reached"""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Ready(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


class EntrypointTestCase(SimpleTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        # a stand-in for gunicorn: the entrypoint knows the web server by the name of the command
        self.gunicorn = Path(directory.name) / "gunicorn"
        self.gunicorn.write_text("#!/bin/sh\necho web server started\n")
        self.gunicorn.chmod(0o755)

    def run_entrypoint(self, *command, **environ):
        env = {"PATH": os.environ["PATH"], "RUN_MIGRATIONS": "0", **environ}
        return subprocess.run(["sh", str(ENTRYPOINT), *command], env=env, capture_output=True, text=True, timeout=30)

    def test_jobs_do_not_wait(self):
        started = time.monotonic()
        result = self.run_entrypoint("echo", "done", WAIT_FOR_URL=f"http://127.0.0.1:{free_port()}/ready")
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "done"))
        self.assertLess(time.monotonic() - started, 5)

    def test_jobs_wait_when_asked_and_give_up(self):
        result = self.run_entrypoint("echo", "done", WAIT_FOR_URL=f"http://127.0.0.1:{free_port()}/ready",
                                     WAIT_FOR_URL_JOBS="1", WAIT_FOR_TIMEOUT="2")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Gave up waiting", result.stderr)
        self.assertNotIn("done", result.stdout)

    def test_the_timeout_bounds_a_stalled_request(self):
        # connections are queued but never accepted: the request hangs until its own timeout
        stalled = socket.socket()
        stalled.bind(("127.0.0.1", 0))
        stalled.listen()
        self.addCleanup(stalled.close)
        started = time.monotonic()
        result = self.run_entrypoint(str(self.gunicorn), WAIT_FOR_URL=f"http://127.0.0.1:{stalled.getsockname()[1]}/",
                                     WAIT_FOR_TIMEOUT="1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Gave up waiting", result.stderr)
        self.assertNotIn("web server started", result.stdout)
        self.assertLess(time.monotonic() - started, 3)

    def test_web_server_waits(self):
        result = self.run_entrypoint(str(self.gunicorn), WAIT_FOR_URL=f"http://127.0.0.1:{free_port()}/ready",
                                     WAIT_FOR_TIMEOUT="2")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Waiting for", result.stdout)
        server = HTTPServer(("127.0.0.1", 0), Ready)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        result = self.run_entrypoint(str(self.gunicorn), WAIT_FOR_URL=f"http://127.0.0.1:{server.server_port}/",
                                     WAIT_FOR_TIMEOUT="10")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("web server started", result.stdout)

    def test_invalid_timeout(self):
        result = self.run_entrypoint("echo", "done", WAIT_FOR_TIMEOUT="abc")
        self.assertEqual(result.returncode, 1)
        self.assertIn("WAIT_FOR_TIMEOUT", result.stderr)
