"""Embedding routing, genuine learned prediction, and offline reproducibility."""
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
from router_attack_demo.selectors import classify_question, fetch_embeddings, train_predictor
from router_attack_demo.workloads import demo_workloads


class SelectorTests(unittest.TestCase):
    def test_embeddings_route_without_keyword_rules_or_network(self):
        for method in ("semantic", "predictor"):
            with self.subTest(method=method), patch.dict(os.environ, {}, clear=True), patch("urllib.request.build_opener") as network:
                _, policy = load_configuration(DEFAULT_ROUTING, "openrouter", False, method)
                # Remove the literal rule match. Embedding methods must still
                # classify the held-out demo questions from their real vectors.
                for rule in policy["policy"]["rules"]:
                    rule["question_contains_any"] = ["UNMATCHABLE PHRASE"]
                routes = [select_question(c["prompt"], policy) for c in demo_workloads()]
                self.assertEqual(routes[0]["rule_id"], "general-assistance")
                self.assertEqual([r["rule_id"] for r in routes[1:]], ["structured-rule-reasoning"] * 2)
                self.assertNotEqual(routes[0]["selected_model"], routes[1]["selected_model"])
                self.assertEqual(routes[1]["attack_model"], routes[0]["selected_model"])
                network.assert_not_called()
                question = demo_workloads()[1]["prompt"]
                self.assertEqual(select_question(question, policy), select_question(question, json.loads(json.dumps(policy))))

    def test_predictor_learns_labels_and_predicts_a_held_out_vector(self):
        examples = [{"vector": [1.0, 0.0], "rule_id": "compact"},
                    {"vector": [0.0, 1.0], "rule_id": "reasoning"}]
        fitted = train_predictor(examples, ["compact", "reasoning"], 100, 1.0, 0.01)
        from router_attack_demo.util import text_digest
        query = "A held-out question with no training label."
        selector = {"method": "predictor", "predictor": fitted,
                    "queries": {text_digest(query): [0.2, 0.98]}}
        self.assertEqual(classify_question(query, selector)[0], "reasoning")
        reversed_examples = copy.deepcopy(examples)
        reversed_examples[0]["rule_id"], reversed_examples[1]["rule_id"] = "reasoning", "compact"
        selector["predictor"] = train_predictor(reversed_examples, ["compact", "reasoning"], 100, 1.0, 0.01)
        self.assertEqual(classify_question(query, selector)[0], "compact")

    def test_unknown_question_does_not_silently_use_keyword_rules(self):
        for method in ("semantic", "predictor"):
            _, policy = load_configuration(DEFAULT_ROUTING, "openrouter", False, method)
            with self.assertRaisesRegex(ValueError, "absent from the embedding cache"):
                select_question("A new question not present in any prepared cache.", policy)

    def test_changed_corpus_invalidates_cache_before_backend_creation(self):
        raw = json.loads(DEFAULT_ROUTING.read_text())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("routing_examples.json", "routing_features.json"):
                (root / name).write_text((DEFAULT_ROUTING.parent / name).read_text())
            examples_path = root / "routing_examples.json"
            examples = json.loads(examples_path.read_text())
            examples[0]["question"] += " changed"
            examples_path.write_text(json.dumps(examples))
            config = root / "routing.json"
            config.write_text(json.dumps(raw))
            with patch("router_attack_demo.cli.ChatBackend") as backend, patch("sys.stderr", io.StringIO()):
                with self.assertRaises(SystemExit):
                    main(["run", "--backend", "api", "--provider", "openrouter", "--selector", "semantic", "--routing-config", str(config)])
                backend.assert_not_called()

    def test_new_selectors_run_and_replay_frozen_policy_without_embedding_calls(self):
        cases = demo_workloads()
        class Backend:
            def __init__(self, base_url, model, **settings): self.model = model
            def generate(self, messages):
                case = next(c for c in cases if c["prompt"] == messages[-1]["content"])
                text = "Study advice."
                if case["kind"] == "quality":
                    answer = case["gold"] if "claude" in self.model else ["incorrect"]
                    text = json.dumps({"answer": answer})
                elif any(AD_SENTENCE in m["content"] for m in messages):
                    text += "\n" + AD_SENTENCE
                return {"text": text, "reported_model": self.model, "latency_seconds": 0,
                        "usage": None, "finish_reason": "test", "request_settings": {}}
        for method in ("semantic", "predictor"):
            with self.subTest(method=method), tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp) / "trace.json"
                with patch.dict(os.environ, {}, clear=True), patch("urllib.request.build_opener") as network, patch("router_attack_demo.cli.ChatBackend", Backend), redirect_stdout(io.StringIO()):
                    self.assertEqual(main(["run", "--backend", "api", "--provider", "openrouter", "--selector", method, "--out", str(out)]), 0)
                    self.assertEqual(main(["replay", "--input", str(out)]), 0)
                    network.assert_not_called()
                payload = json.loads(out.read_text())
                self.assertEqual(payload["routing_policy"]["selector"]["method"], method)
                self.assertEqual(payload["model_calls_attempted"], 6)
                self.assertEqual(len(payload["runs"]), 7)

    def test_embedding_http_errors_do_not_echo_credentials(self):
        import urllib.error
        encoder = {"base_url": "https://example.com/v1", "model": "embedding-model", "key_env": "TEST_EMBEDDING_KEY"}
        secret = "example-secret-for-test-only"
        failure = urllib.error.HTTPError("https://example.com/v1/embeddings", 429, secret, {}, io.BytesIO(secret.encode()))
        with patch.dict(os.environ, {"TEST_EMBEDDING_KEY": secret}), patch("urllib.request.build_opener") as opener:
            opener.return_value.open.side_effect = failure
            with self.assertRaisesRegex(RuntimeError, "HTTP 429") as error:
                fetch_embeddings(encoder, ["A question"])
            self.assertNotIn(secret, str(error.exception))
            self.assertEqual(opener.return_value.open.call_count, 1)


if __name__ == "__main__": unittest.main()
