"""Credential handling, endpoint restrictions, and bounded failure behavior."""
import io
import hashlib
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
from router_attack_demo.cli import DEFAULT_ROUTING, DEFAULT_TRACE, main
from router_attack_demo.report import score_records, terminal_report
from router_attack_demo.routing import load_configuration
from router_attack_demo.runner import run_experiment, write_json
from router_attack_demo.workloads import demo_workloads


class BackendTests(unittest.TestCase):
    def test_credential_values_cannot_be_used_as_environment_variable_names(self):
        value = "a-placeholder-secret-with-hyphens"
        with self.assertRaises(ValueError) as error:
            ChatBackend("https://example.org/v1", "model", key_env=value)
        self.assertNotIn(value, str(error.exception))

    def test_successful_responses_do_not_echo_credentials(self):
        secret = "unit-test-success-response-secret"
        for location in ("content", "metadata"):
            reply = {"model": "model", "choices": [{"message": {"content": "answer"}, "finish_reason": "stop"}]}
            if location == "content": reply["choices"][0]["message"]["content"] = secret
            else: reply["usage"] = {"unexpected_field": secret}
            with self.subTest(location=location), patch.dict(os.environ, {"TEST_KEY": secret}), patch("urllib.request.build_opener") as opener:
                opener.return_value.open.return_value.__enter__.return_value.read.return_value = json.dumps(reply).encode()
                backend = ChatBackend("https://example.org/v1", "model", key_env="TEST_KEY")
                with self.assertRaises(BackendError) as error: backend.generate([])
                self.assertNotIn(secret, str(error.exception))

    def test_only_token_counts_are_retained_from_provider_metadata(self):
        reply = {"model": "model", "choices": [{"message": {"content": "answer"}, "finish_reason": "stop"}],
                 "id": "unnecessary-correlation-id", "system_fingerprint": "unnecessary-fingerprint",
                 "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3,
                           "billing_account": "unnecessary-account", "cost": 123}}
        with patch.dict(os.environ, {"TEST_KEY": "unit-test-key"}), patch("urllib.request.build_opener") as opener:
            opener.return_value.open.return_value.__enter__.return_value.read.return_value = json.dumps(reply).encode()
            generation = ChatBackend("https://example.org/v1", "model", key_env="TEST_KEY").generate([])
        self.assertEqual(generation["usage"], {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3})
        self.assertNotIn("unnecessary-", json.dumps(generation))

    def test_trace_writes_do_not_follow_predictable_temp_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            out, other = folder / "trace.json", folder / "unrelated.txt"
            other.write_text("leave unchanged")
            out.with_suffix(".json.tmp").symlink_to(other)
            write_json(out, {"result": "saved"})
            self.assertEqual(other.read_text(), "leave unchanged")
            self.assertEqual(json.loads(out.read_text()), {"result": "saved"})
            self.assertEqual(list(folder.glob(".router-trace-*.tmp")), [])

    def test_concurrent_output_creation_stops_before_inference(self):
        original_open = os.open
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "trace.json"
            models, policy = load_configuration(DEFAULT_ROUTING, "openrouter", False)
            def race(path, flags, mode):
                if Path(path) == out: out.write_text("other process owns this file")
                return original_open(path, flags, mode)
            with patch("router_attack_demo.runner.os.open", side_effect=race):
                with self.assertRaisesRegex(ValueError, "Output already exists"):
                    run_experiment(demo_workloads(), {m["id"]: object() for m in models},
                                   list(ATTACKS), out, "test", {}, routing_policy=policy)
            self.assertEqual(out.read_text(), "other process owns this file")

    def test_terminal_output_escapes_controls_from_model_text(self):
        payload = json.loads(DEFAULT_TRACE.read_text())
        records = score_records(payload)
        records[0]["delivered_response"] = "advice\x1b]52;c;ZXhhbXBsZQ==\x07\x1b[2J\u202e"
        rendered = terminal_report(payload, records, details=True)
        for control in ("\x1b", "\x07", "\u202e"): self.assertNotIn(control, rendered)
        self.assertIn("\\u001b", rendered)

    def test_downloads_are_bounded_and_do_not_follow_temp_symlinks(self):
        from scripts.prepare_models import download
        data = b"abcd"
        checksum = hashlib.sha256(data).hexdigest()
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            folder = Path(directory)
            out, other = folder / "model.gguf", folder / "unrelated.txt"
            other.write_text("leave unchanged")
            out.with_suffix(".gguf.part").symlink_to(other)
            with patch("urllib.request.urlopen", return_value=io.BytesIO(data)):
                download("https://example.org/model", out, checksum, len(data))
            self.assertEqual(out.read_bytes(), data)
            self.assertEqual(other.read_text(), "leave unchanged")
            out.unlink()
            with patch("urllib.request.urlopen", return_value=io.BytesIO(data + b"too much")):
                with self.assertRaisesRegex(RuntimeError, "exceeded"):
                    download("https://example.org/model", out, checksum, len(data))
            self.assertFalse(out.exists())
            self.assertEqual(list(folder.glob(".model-download-*.part")), [])
            def racing_download(request, timeout):
                out.write_bytes(b"other process owns this file")
                return io.BytesIO(data)
            with patch("urllib.request.urlopen", side_effect=racing_download):
                with self.assertRaises(FileExistsError):
                    download("https://example.org/model", out, checksum, len(data))
            self.assertEqual(out.read_bytes(), b"other process owns this file")

    def test_invalid_temperature_is_refused_before_backend_creation(self):
        for value in ("nan", "inf", "-0.1", "2.1"):
            with self.subTest(value=value), patch("router_attack_demo.cli.ChatBackend") as backend, patch("sys.stderr", io.StringIO()):
                with self.assertRaises(SystemExit): main(["run", "--backend", "api", "--temperature", value])
                backend.assert_not_called()

    def test_endpoint_and_redirect_boundaries(self):
        self.assertEqual(validate_url("https://example.org/v1/", False), "https://example.org/v1")
        self.assertEqual(validate_url("http://127.0.0.1:8081/v1", True), "http://127.0.0.1:8081/v1")
        for value, local in [("http://example.org/v1", False), ("https://key@example.org/v1", False),
                             ("https://example.org/v1?key=x", False), ("https://example.org/v1#x", False),
                             ("https://example.org:invalid/v1", False), ("https://example.org:99999/v1", False),
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
