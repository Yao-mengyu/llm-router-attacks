"""Question-dependent routing over a configurable model catalog.

Capability labels are endorsed policy configuration, not model certifications.
The selector sees the client question, never the gold answer or model output.
"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path

from .backends import validate_url
from .util import digest, text_digest

SCHEMA = "llm-router-demo-routing-v1"


def labels(value, field):
    if (not isinstance(value, list) or not value
            or any(not isinstance(item, str) or not item.strip() for item in value)
            or len(set(value)) != len(value)):
        raise ValueError(f"{field} must be a nonempty list of distinct strings.")
    return list(value)


def validate_policy(policy):
    if not isinstance(policy, dict) or set(policy) != {"rules", "default"}:
        raise ValueError("Routing policy needs rules and a default rule.")
    rules = policy["rules"]
    if not isinstance(rules, list):
        raise ValueError("Routing rules must be a list.")
    normalized, ids = [], set()
    for rule, default in [(r, False) for r in rules] + [(policy["default"], True)]:
        fields = {"id", "required_capabilities"} | (set() if default else {"question_contains_any"})
        if (not isinstance(rule, dict) or set(rule) != fields
                or not isinstance(rule["id"], str) or not rule["id"].strip()
                or rule["id"] in ids):
            raise ValueError("Routing rules need distinct IDs and supported fields.")
        ids.add(rule["id"])
        item = {"id": rule["id"], "required_capabilities": labels(rule["required_capabilities"], "Required capabilities")}
        if not default:
            item["question_contains_any"] = labels(rule["question_contains_any"], "Question patterns")
        normalized.append(item)
    return {"rules": normalized[:-1], "default": normalized[-1]}


def load_configuration(path: Path, provider: str, local: bool, selector: str = "rules"):
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("schema") != SCHEMA:
        raise ValueError("Unsupported routing configuration schema.")
    if set(raw) - {"schema", "policy", "providers", "selectors"}:
        raise ValueError("Routing configuration has unsupported fields.")
    policy = validate_policy(raw["policy"])
    if not isinstance(raw["providers"], dict) or provider not in raw["providers"]:
        raise ValueError("Provider profile is absent from the routing configuration.")
    profile = raw["providers"][provider]
    if not isinstance(profile, dict) or set(profile) - {"base_url", "key_env", "token_parameter", "models"}:
        raise ValueError("Provider profile has unsupported fields; credentials belong in environment variables.")
    entries = profile.get("models")
    if not isinstance(entries, list) or not entries:
        raise ValueError("Provider profile needs a nonempty model pool.")
    models, ids = [], set()
    for entry in entries:
        if (not isinstance(entry, dict)
                or set(entry) - {"id", "capabilities", "preference", "base_url", "key_env", "token_parameter"}):
            raise ValueError("Model catalog entry has unsupported fields.")
        model = entry.get("id")
        preference = entry.get("preference", 0)
        if not isinstance(model, str) or not model.strip() or model in ids:
            raise ValueError("Model IDs must be nonempty and distinct within a provider profile.")
        if type(preference) is not int:
            raise ValueError("Model preference must be an integer; lower values are preferred.")
        ids.add(model)
        key_env = entry.get("key_env", profile.get("key_env", "OPENAI_API_KEY"))
        if not isinstance(key_env, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key_env):
            raise ValueError("key_env must name an environment variable.")
        token_parameter = entry.get("token_parameter", profile.get("token_parameter", "max_completion_tokens"))
        if token_parameter not in {"max_tokens", "max_completion_tokens"}:
            raise ValueError("Unsupported token limit parameter in provider profile.")
        endpoint = entry.get("base_url", profile.get("base_url"))
        if not isinstance(endpoint, str):
            raise ValueError("Every model needs a configured endpoint.")
        models.append({"id": model, "capabilities": labels(entry.get("capabilities"), "Model capabilities"),
                       "preference": preference, "base_url": validate_url(endpoint, local),
                       "key_env": key_env, "token_parameter": token_parameter})
    snapshot = {"schema": SCHEMA, "policy": policy,
                "models": [{k: m[k] for k in ("id", "capabilities", "preference")} for m in models]}
    if selector not in {"rules", "semantic", "predictor"}:
        raise ValueError("Selector must be rules, semantic, or predictor.")
    if selector != "rules":
        from .selectors import load_selector
        snapshot["selector"] = load_selector(path, raw, selector, policy)
    return models, snapshot


def select_question(question: str, snapshot: dict):
    if not isinstance(question, str) or not question.strip():
        raise ValueError("Routing requires a nonempty client question.")
    policy = validate_policy(snapshot["policy"])
    rule = next((r for r in policy["rules"]
                 if any(pattern.casefold() in question.casefold() for pattern in r["question_contains_any"])),
                policy["default"])
    explanation = None
    if "selector" in snapshot:
        from .selectors import classify_question
        category, explanation = classify_question(question, snapshot["selector"])
        rule = next(r for r in [*policy["rules"], policy["default"]] if r["id"] == category)
    required = set(rule["required_capabilities"])
    pool = sorted(snapshot["models"], key=lambda m: (m["preference"], m["id"]))
    eligible = [m["id"] for m in pool if required <= set(m["capabilities"])]
    if not eligible:
        raise ValueError(f"No model satisfies routing rule {rule['id']} for this question.")
    outside = [m["id"] for m in pool if m["id"] not in eligible]
    decision = {"question_hash": text_digest(question), "policy_hash": digest(snapshot),
            "rule_id": rule["id"], "required_capabilities": copy.deepcopy(rule["required_capabilities"]),
            "eligible_models": eligible, "selected_model": eligible[0],
            "attack_model": outside[0] if outside else None}
    if explanation is not None:
        decision.update(selector=snapshot["selector"]["method"], routing_scores=explanation)
    return decision
