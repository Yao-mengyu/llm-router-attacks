"""Credential handling, endpoint restrictions, and bounded failure behavior."""
import io
import json
import os
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from attacks import ATTACKS
from router_attack_demo.backends import BackendError, ChatBackend, NoRedirect, validate_url
from router_attack_demo.cli import DEFAULT_ROUTING, main
from router_attack_demo.routing import load_configuration
from router_attack_demo.runner import run_experiment
from router_attack_demo.workloads import demo_workloads


class BackendTests(unittest.TestCase):
    def test_endpoint_and_redirect_boundaries(self):
        self.assertEqual(validate_url("https://example.org/v1/", False), "https://example.org/v1")
        self.assertEqual(validate_url("http://127.0.0.1:8081/v1", True), "http://127.0.0.1:8081/v1")
        for value, local in [("http://example.org/v1", False), ("https://key@example.org/v1", False),
                             ("https://example.org/v1?key=x", False), ("https://example.org/v1#x", False),
                             ("http://example.org/v1", True), ("https://localhost/v1", True)]:
            with self.assertRaises(ValueError): validate_url(value, local)
        with self.assertRaises(BackendError): NoRedirect().redirect_request(None, None, 302, None, None, "https://other.org")

    def test_no_key_required_for_dry_run_and_no_network(self):
        with patch.dict(os.environ, {}, clear=True), patch("urllib.request.build_opener") as opener, redirect_stdout(io.StringIO()):
            self.assertEqual(main(["run", "--backend", "api", "--dry-run"]), 0)
            opener.assert_not_called()

    def test_api_errors_do_not_echo_credentials(self):
        secret = "unit-test-secret-do-not-log"
        with patch.dict(os.environ, {"TEST_KEY": secret}), patch("urllib.request.build_opener") as opener:
            backend = ChatBackend("https://example.org/v1", "model", key_env="TEST_KEY")
            opener.return_value.open.side_effect = urllib.error.HTTPError(
                "https://example.org", 401, secret, {}, io.BytesIO(secret.encode()))
            with self.assertRaises(BackendError) as error: backend.generate([])
            self.assertNotIn(secret, str(error.exception))
            self.assertNotIn(secret, repr(backend))

    def test_call_budget_refuses_before_backend_creation(self):
        with patch("router_attack_demo.cli.ChatBackend") as backend, redirect_stdout(io.StringIO()), patch("sys.stderr", io.StringIO()):
            with self.assertRaises(SystemExit): main(["run", "--backend", "api", "--max-calls", "1"])
            backend.assert_not_called()

    def test_failed_prerequisites_are_retained_without_extra_calls(self):
        class Failing:
            def __init__(self, model): self.model = model
            def generate(self, messages): raise BackendError("intentional unit-test failure")
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()):
            models, policy = load_configuration(DEFAULT_ROUTING, "openrouter", False)
            pool = {m["id"]: Failing(m["id"]) for m in models}
            data = run_experiment(demo_workloads(), pool, list(ATTACKS),
                                  Path(tmp) / "run.json", "unit_test", {}, routing_policy=policy)
            self.assertEqual(data["model_calls_attempted"], 6)
            self.assertEqual(len(data["runs"]), 0)
            self.assertEqual(len(data["errors"]), 7)


if __name__ == "__main__": unittest.main()
