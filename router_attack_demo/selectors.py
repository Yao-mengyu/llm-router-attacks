"""Embedding retrieval and a supervised linear routing predictor.

All features are frozen before the attack. Selection and recorded replay are
offline; the separate preparation command makes one explicit embedding call.
"""
from __future__ import annotations

import json
import math
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

from .backends import BackendError, NoRedirect, reject_credential_echo, token_usage, validate_url
from .util import canonical, digest, text_digest


def unit_vector(value):
    try:
        valid = (isinstance(value, list) and 0 < len(value) <= 8192
                 and all(type(v) in (int, float) and math.isfinite(v) for v in value))
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError("Embedding must contain finite numeric values.")
    scale = max(abs(v) for v in value)
    if scale == 0:
        raise ValueError("Embedding must have nonzero length.")
    scaled = [v / scale for v in value]
    norm = math.sqrt(sum(v * v for v in scaled))
    return [v / norm for v in scaled]


def dot(a, b):
    if len(a) != len(b):
        raise ValueError("Embedding dimensions differ.")
    return sum(x * y for x, y in zip(a, b))


def softmax(values):
    offset = max(values)
    exp = [math.exp(v - offset) for v in values]
    total = sum(exp)
    return [v / total for v in exp]


def train_predictor(examples, classes, epochs, learning_rate, l2):
    """Full-batch multinomial logistic regression on normalized embeddings."""
    dimension = len(examples[0]["vector"])
    weights = [[0.0] * dimension for _ in classes]
    bias = [0.0] * len(classes)
    for _ in range(epochs):
        gradients = [[0.0] * dimension for _ in classes]
        offsets = [0.0] * len(classes)
        for example in examples:
            vector = example["vector"]
            probabilities = softmax([dot(w, vector) + b for w, b in zip(weights, bias)])
            for i, label in enumerate(classes):
                error = probabilities[i] - int(example["rule_id"] == label)
                offsets[i] += error
                gradients[i] = [g + error * v for g, v in zip(gradients[i], vector)]
        for i in range(len(classes)):
            weights[i] = [w - learning_rate * (g / len(examples) + l2 * w)
                          for w, g in zip(weights[i], gradients[i])]
            bias[i] -= learning_rate * offsets[i] / len(examples)
    return {"classes": classes, "weights": weights, "bias": bias}


def read_selector_inputs(path: Path, raw: dict, method: str, policy: dict):
    configured = raw.get("selectors", {}).get(method)
    if not isinstance(configured, dict):
        raise ValueError(f"Configure selectors.{method} before using this selector.")
    allowed = {"encoder", "examples", "cache", "k", "epochs", "learning_rate", "l2"}
    if set(configured) - allowed:
        raise ValueError("Selector configuration contains unsupported fields.")
    encoder = configured.get("encoder")
    if not isinstance(encoder, dict) or set(encoder) != {"base_url", "model", "key_env"}:
        raise ValueError("Encoder needs base_url, model, and key_env.")
    validate_url(encoder["base_url"], local=encoder["base_url"].startswith("http://"))
    if (not isinstance(encoder["model"], str) or not encoder["model"].strip()
            or not isinstance(encoder["key_env"], str)
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", encoder["key_env"])):
        raise ValueError("Invalid embedding model or credential environment variable name.")
    examples = json.loads((path.parent / configured["examples"]).read_text(encoding="utf-8"))
    classes = sorted([r["id"] for r in policy["rules"]] + [policy["default"]["id"]])
    if (not isinstance(examples, list) or not examples or len(examples) > 128
            or any(not isinstance(e, dict) or set(e) != {"question", "rule_id"}
                   or not isinstance(e["question"], str) or not e["question"].strip()
                   or e["rule_id"] not in classes for e in examples)
            or set(e["rule_id"] for e in examples) != set(classes)
            or len({e["question"] for e in examples}) != len(examples)):
        raise ValueError("Routing examples must be distinct questions covering every policy category.")
    return configured, encoder, examples, classes


def load_selector(path: Path, raw: dict, method: str, policy: dict):
    configured, encoder, examples, classes = read_selector_inputs(path, raw, method, policy)
    cache = json.loads((path.parent / configured["cache"]).read_text(encoding="utf-8"))
    if (cache.get("schema") != "router-attack-demo-embeddings-v1"
            or cache.get("encoder") != {"base_url": encoder["base_url"].rstrip("/"), "model": encoder["model"]}
            or cache.get("examples_hash") != digest(examples)):
        raise ValueError("Embedding cache does not match the encoder or routing examples; rebuild it.")
    vectors = {key: unit_vector(value) for key, value in cache["vectors"].items()}
    dimensions = {len(v) for v in vectors.values()}
    if len(dimensions) != 1:
        raise ValueError("Cached embeddings have inconsistent dimensions.")
    training = [{"rule_id": e["rule_id"], "question_hash": text_digest(e["question"]),
                 "vector": vectors[text_digest(e["question"])]} for e in examples]
    selector = {"method": method, "encoder": cache["encoder"],
                "examples": training, "queries": vectors}
    if method == "semantic":
        k = configured.get("k", 3)
        if type(k) is not int or not 1 <= k <= len(training):
            raise ValueError("Semantic k must be between 1 and the number of examples.")
        selector["k"] = k
    elif method == "predictor":
        epochs = configured.get("epochs", 200)
        rate, l2 = configured.get("learning_rate", 1.0), configured.get("l2", 0.01)
        if (type(epochs) is not int or not 1 <= epochs <= 1000
                or type(rate) not in (float, int) or not 0 < rate <= 10
                or type(l2) not in (float, int) or not 0 <= l2 <= 1):
            raise ValueError("Invalid predictor training parameters.")
        selector["training"] = {"epochs": epochs, "learning_rate": rate, "l2": l2}
        selector["predictor"] = train_predictor(training, classes, epochs, rate, l2)
    return selector


def classify_question(question: str, selector: dict):
    key = text_digest(question)
    if key not in selector["queries"]:
        raise ValueError("Question is absent from the embedding cache; run scripts/prepare_routing.py with this question first.")
    vector = selector["queries"][key]
    if selector["method"] == "semantic":
        neighbors = sorted(selector["examples"], key=lambda e: (-dot(vector, e["vector"]), e["question_hash"]))[:selector["k"]]
        classes = sorted({e["rule_id"] for e in selector["examples"]})
        votes = {label: sum(e["rule_id"] == label for e in neighbors) for label in classes}
        similarities = {label: sum(dot(vector, e["vector"]) for e in neighbors if e["rule_id"] == label) for label in classes}
        label = min(classes, key=lambda name: (-votes[name], -similarities[name], name))
        return label, {"neighbor_votes": votes, "neighbors": [e["question_hash"] for e in neighbors]}
    if selector["method"] == "predictor":
        predictor = selector["predictor"]
        scores = softmax([dot(w, vector) + b for w, b in zip(predictor["weights"], predictor["bias"])])
        probabilities = dict(zip(predictor["classes"], scores))
        label = min(probabilities, key=lambda name: (-probabilities[name], name))
        return label, {"category_probabilities": probabilities}
    raise ValueError("Unsupported embedding selector method.")


def fetch_embeddings(encoder: dict, questions: list[str], timeout=90):
    """One bounded request, no retries, redirects, or provider error bodies."""
    local = encoder["base_url"].startswith("http://")
    base_url = validate_url(encoder["base_url"], local)
    headers = {"Content-Type": "application/json", "User-Agent": "Router-Attack-Demo/0.3"}
    key = ""
    if not local:
        key = os.environ.get(encoder["key_env"], "")
        if not key.strip():
            raise ValueError(f"Set {encoder['key_env']} before preparing routing embeddings.")
        headers["Authorization"] = "Bearer " + key
    request = urllib.request.Request(base_url + "/embeddings", method="POST", headers=headers,
        data=canonical({"model": encoder["model"], "input": questions, "encoding_format": "float"}))
    handlers = [NoRedirect()]
    if local:
        handlers.append(urllib.request.ProxyHandler({}))
    try:
        with urllib.request.build_opener(*handlers).open(request, timeout=timeout) as response:
            body = response.read(8_000_001)
            if len(body) > 8_000_000:
                raise BackendError("Embedding response exceeds 8 MB.")
            raw = json.loads(body)
    except urllib.error.HTTPError as exc:
        status = exc.code
        exc.close()
        raise BackendError(f"Embedding endpoint returned HTTP {status}; response body omitted.") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise BackendError("Embedding connection failed or timed out.") from None
    except (ValueError, UnicodeError):
        raise BackendError("Embedding endpoint returned invalid JSON.") from None
    reject_credential_echo(raw, key)
    try:
        rows = sorted(raw["data"], key=lambda e: e["index"])
        if [e["index"] for e in rows] != list(range(len(questions))):
            raise ValueError("Incomplete embedding batch.")
        vectors = [unit_vector(e["embedding"]) for e in rows]
        if len({len(v) for v in vectors}) != 1:
            raise ValueError("Embedding dimensions differ.")
    except (KeyError, TypeError, ValueError):
        raise BackendError("Embedding endpoint returned an invalid or incomplete batch.") from None
    return vectors, {"reported_model": raw.get("model"), "usage": token_usage(raw.get("usage")), "requests": 1}
