"""Question-dependent routing, configurable catalogs, and attack execution."""
import copy
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from router_attack_demo.cli import DEFAULT_ROUTING, main
from router_attack_demo.util import AD_SENTENCE
from router_attack_demo.routing import load_configuration, select_question
from router_attack_demo.runner import run_experiment
from router_attack_demo.workloads import demo_workloads


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.models, self.policy = load_configuration(DEFAULT_ROUTING, "openrouter", False)
        self.cases = demo_workloads()

    def test_same_pool_routes_general_and_rule_questions_differently(self):
        routes = {c["id"]: select_question(c["prompt"], self.policy) for c in self.cases}
        study = routes["study-no-products"]
        self.assertEqual(study["selected_model"], "meta-llama/llama-3.2-3b-instruct")
        self.assertEqual(set(study["eligible_models"]), {"anthropic/claude-opus-4.6", "meta-llama/llama-3.2-3b-instruct"})
        self.assertIsNone(study["attack_model"])
        for name in ("access-policy", "stable-ranking"):
            self.assertEqual(routes[name]["selected_model"], "anthropic/claude-opus-4.6")
            self.assertEqual(routes[name]["eligible_models"], ["anthropic/claude-opus-4.6"])
            self.assertEqual(routes[name]["attack_model"], "meta-llama/llama-3.2-3b-instruct")

    def test_custom_catalog_supports_multiple_eligible_models(self):
        policy = copy.deepcopy(self.policy)
        policy["models"] = [
            {"id": "compact", "capabilities": ["general"], "preference": 1},
            {"id": "reasoner-a", "capabilities": ["general", "structured_reasoning"], "preference": 5},
            {"id": "reasoner-b", "capabilities": ["general", "structured_reasoning"], "preference": 3},
        ]
        route = select_question(self.cases[1]["prompt"], policy)
        self.assertEqual(route["eligible_models"], ["reasoner-b", "reasoner-a"])
        self.assertEqual(route["selected_model"], "reasoner-b")
        self.assertEqual(route["attack_model"], "compact")

    def test_rules_use_question_text_without_case_id_kind_or_gold(self):
        first = select_question("Sort by score descending, then cost ascending.", self.policy)
        second = select_question("Give me study advice.", self.policy)
        self.assertEqual(first["rule_id"], "structured-rule-reasoning")
        self.assertEqual(second["rule_id"], "general-assistance")
        self.assertNotEqual(first["selected_model"], second["selected_model"])

    def test_dry_run_prints_per_question_routes_without_credentials_or_network(self):
        output = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), patch("router_attack_demo.cli.ChatBackend") as backend, redirect_stdout(output):
            self.assertEqual(main(["run", "--backend", "api", "--provider", "openrouter", "--dry-run"]), 0)
            backend.assert_not_called()
        plan = json.loads(output.getvalue())
        self.assertEqual(plan["max_call_bound"], 6)
        self.assertEqual({r["selected_model"] for r in plan["question_routes"]},
                         {"anthropic/claude-opus-4.6", "meta-llama/llama-3.2-3b-instruct"})

    def test_attack_plan_with_no_ineligible_candidate_fails_before_api_calls(self):
        config = json.loads(DEFAULT_ROUTING.read_text())
        for model in config["providers"]["openrouter"]["models"]:
            if "structured_reasoning" not in model["capabilities"]:
                model["capabilities"].append("structured_reasoning")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "routing.json"
            path.write_text(json.dumps(config))
            with patch("router_attack_demo.cli.ChatBackend") as backend, patch("sys.stderr", io.StringIO()):
                with self.assertRaises(SystemExit):
                    main(["run", "--backend", "api", "--provider", "openrouter", "--routing-config", str(path)])
                backend.assert_not_called()

    def test_cli_executes_policy_choices_and_exports_routes(self):
        cases = self.cases
        calls, credentials = [], []

        class Backend:
            def __init__(self, base_url, model, **settings):
                self.model = model
                credentials.append(settings["key_env"])
            def generate(self, messages):
                case = next(c for c in cases if c["prompt"] == messages[-1]["content"])
                calls.append((case["id"], self.model))
                if case["kind"] == "quality":
                    answer = case["gold"] if self.model == "anthropic/claude-opus-4.6" else case["gold"][:1]
                    text = json.dumps({"answer": answer})
                else:
                    text = "Three study steps."
                    if any(AD_SENTENCE in m["content"] for m in messages): text += "\n" + AD_SENTENCE
                return {"text": text, "reported_model": self.model, "latency_seconds": 0,
                        "usage": None, "finish_reason": "test", "request_settings": {}}

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "run.json"
            report = Path(tmp) / "REPORT.md"
            display = io.StringIO()
            with patch.dict(os.environ, {}, clear=True), patch("router_attack_demo.cli.ChatBackend", Backend):
                with redirect_stdout(display):
                    self.assertEqual(main(["run", "--backend", "api", "--provider", "openrouter", "--out", str(out)]), 0)
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(main(["report", "--input", str(out), "--out", str(report)]), 0)
            self.assertEqual(len(calls), 6)
            self.assertEqual(credentials, ["OPENROUTER_API_KEY", "OPENROUTER_API_KEY"])
            self.assertEqual(calls[:2], [("study-no-products", "meta-llama/llama-3.2-3b-instruct")] * 2)
            payload = json.loads(out.read_text())
            records = {r["id"]: r for r in payload["runs"]}
            normal = records["study-no-products/honest"]
            injected = records["study-no-products/request_injection"]
            tampered = records["study-no-products/response_tampering"]
            self.assertEqual(injected["client_messages"], normal["client_messages"])
            self.assertNotEqual(injected["backend_messages"], normal["backend_messages"])
            self.assertIn(AD_SENTENCE, injected["backend_response"])
            self.assertEqual(tampered["backend_messages"], normal["backend_messages"])
            self.assertEqual(tampered["generation"], normal["generation"])
            self.assertNotIn(AD_SENTENCE, tampered["backend_response"])
            self.assertEqual(tampered["delivered_response"], normal["backend_response"] + "\n\n" + AD_SENTENCE)
            for case in ("access-policy", "stable-ranking"):
                baseline = records[case + "/honest"]
                substituted = records[case + "/model_selection"]
                self.assertEqual(baseline["backend_messages"], substituted["backend_messages"])
                self.assertEqual(substituted["returned_model"], baseline["actual_model"])
                self.assertNotEqual(substituted["actual_model"], substituted["returned_model"])
                self.assertTrue(baseline["evaluation"]["task_pass"])
                self.assertFalse(substituted["evaluation"]["task_pass"])
            content = report.read_text()
            self.assertIn("## Question-based routing", content)
            self.assertIn("general-assistance", content)
            self.assertIn("structured-rule-reasoning", content)
            self.assertIn("2/2 approved vs. 0/2 unauthorized", content)

    def test_routing_policy_is_saved_before_inference(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "run.json"
            cases = self.cases
            expected_policy = self.policy

            class Backend:
                def __init__(self, model): self.model = model
                def generate(self, messages):
                    saved = json.loads(out.read_text())
                    if saved["routing_policy"] != expected_policy:
                        raise AssertionError("Routing policy was not saved before inference.")
                    case = next(c for c in cases if c["prompt"] == messages[-1]["content"])
                    text = json.dumps({"answer": case["gold"]}) if case["kind"] == "quality" else "Three study steps."
                    if any(AD_SENTENCE in m["content"] for m in messages): text += "\n" + AD_SENTENCE
                    return {"text": text, "reported_model": self.model, "latency_seconds": 0,
                            "usage": None, "finish_reason": "test", "request_settings": {}}

            pool = {m["id"]: Backend(m["id"]) for m in self.models}
            with redirect_stdout(io.StringIO()):
                payload = run_experiment(cases, pool,
                    ["model_selection", "request_injection", "response_tampering"], out, "unit_test", {},
                    routing_policy=self.policy)
            self.assertEqual(payload["model_calls_attempted"], 6)
            self.assertEqual(len(payload["runs"]), 7)
            for record in payload["runs"]:
                if record["case_kind"] == "study":
                    self.assertEqual(record["actual_model"], "meta-llama/llama-3.2-3b-instruct")
                if record["scenario"] == "model_selection":
                    self.assertNotEqual(record["returned_model"], record["actual_model"])
                    self.assertNotIn(record["actual_model"], record["eligible_models"])


if __name__ == "__main__": unittest.main()
