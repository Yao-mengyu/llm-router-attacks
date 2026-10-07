#!/usr/bin/env python3
"""Prepare real semantic features in one explicitly requested embedding batch."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from router_attack_demo.util import digest, text_digest
from router_attack_demo.routing import validate_policy
from router_attack_demo.selectors import fetch_embeddings, read_selector_inputs
from router_attack_demo.workloads import demo_workloads


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--routing-config", type=Path, default=ROOT / "configs/routing.json")
    parser.add_argument("--selector", choices=("semantic", "predictor"), default="semantic")
    parser.add_argument("--questions", type=Path, help="Optional JSON list of additional question strings.")
    parser.add_argument("--out", type=Path, help="Override configured cache path; existing files are never overwritten.")
    parser.add_argument("--dry-run", action="store_true", help="Inspect one-call plan without credentials or network.")
    args = parser.parse_args(argv)
    try:
        raw = json.loads(args.routing_config.read_text(encoding="utf-8"))
        configured, encoder, examples, _ = read_selector_inputs(args.routing_config, raw, args.selector, validate_policy(raw["policy"]))
        queries = [c["prompt"] for c in demo_workloads()]
        if args.questions:
            additional = json.loads(args.questions.read_text(encoding="utf-8"))
            if not isinstance(additional, list) or any(not isinstance(q, str) or not q.strip() for q in additional):
                raise ValueError("Questions file must be a JSON list of nonempty strings.")
            queries.extend(additional)
        texts = list(dict.fromkeys([e["question"] for e in examples] + queries))
        if len(texts) > 128 or sum(len(t) for t in texts) > 100_000:
            raise ValueError("Preparation is limited to 128 texts and 100,000 total characters.")
        out = args.out or args.routing_config.parent / configured["cache"]
        plan = {"endpoint": encoder["base_url"], "embedding_model": encoder["model"],
                "embedding_calls": 1, "chat_model_calls": 0, "texts": len(texts), "out": str(out)}
        if args.dry_run:
            print(json.dumps(plan, indent=2)); return 0
        if out.exists():
            raise ValueError("Embedding cache exists; choose a new --out path and update the configuration.")
        vectors, metadata = fetch_embeddings(encoder, texts)
        cache = {"schema": "router-attack-demo-embeddings-v1",
                 "generated_at": datetime.now(timezone.utc).isoformat(),
                 "encoder": {"base_url": encoder["base_url"].rstrip("/"), "model": encoder["model"]},
                 "examples_hash": digest(examples), "preparation": metadata,
                 "vectors": {text_digest(t): v for t, v in zip(texts, vectors)}}
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("x", encoding="utf-8") as stream:
            json.dump(cache, stream, indent=2); stream.write("\n")
        print(json.dumps(plan, indent=2))
        return 0
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        parser.error(str(exc))


if __name__ == "__main__": raise SystemExit(main())
