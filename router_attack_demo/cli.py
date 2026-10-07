"""Replay the attacks, run local/API models, and export the results."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from attacks import ATTACKS
from .backends import ChatBackend
from .report import markdown_report, score_records, terminal_report
from .runner import TRACE_SCHEMA, run_experiment
from .routing import load_configuration, select_question
from .workloads import demo_workloads

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TRACE = ROOT / "examples" / "api" / "trace.json"
DEFAULT_ROUTING = ROOT / "configs" / "routing.json"


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("replay", "report"):
        q = sub.add_parser(name)
        q.add_argument("--input", type=Path, default=DEFAULT_TRACE)
        if name == "replay":
            q.add_argument("--attack", choices=["all", *ATTACKS], default="all")
            q.add_argument("--details", action="store_true")
        if name == "report":
            q.add_argument("--out", type=Path, required=True)
    q = sub.add_parser("run")
    q.add_argument("--backend", choices=("api", "local"), required=True)
    q.add_argument("--attack", choices=["all", *ATTACKS], default="all")
    q.add_argument("--case", action="append", dest="case_ids", help="Run specific fixed case IDs; repeatable.")
    q.add_argument("--provider", help="Provider profile in the routing configuration; defaults to openrouter or local.")
    q.add_argument("--routing-config", type=Path, default=DEFAULT_ROUTING,
                   help="Model catalog and question-based routing policy JSON.")
    q.add_argument("--selector", choices=("rules", "semantic", "predictor"), default="rules",
                   help="Question selector; embedding methods use the prepared cache without network calls.")
    q.add_argument("--key-env", help="Override the profile's credential environment variable name.")
    q.add_argument("--max-tokens", type=int, default=384)
    q.add_argument("--max-calls", type=int, default=6, help="Refuse a plan whose maximum exceeds this call budget.")
    q.add_argument("--model-claim", choices=("approved", "actual"), default="approved",
                   help="Model-selection variant: falsely claim the approved model (default), or return the actual identity.")
    q.add_argument("--temperature", type=float, default=0.0)
    q.add_argument("--omit-temperature", action="store_true", help="For APIs whose models reject temperature.")
    q.add_argument("--seed", type=int, help="Omitted by default; provider support varies.")
    q.add_argument("--token-parameter", choices=("max_tokens", "max_completion_tokens"))
    q.add_argument("--timeout", type=float, default=90.0)
    q.add_argument("--out", type=Path, default=Path("results/run.json"))
    q.add_argument("--dry-run", action="store_true", help="Print fixed cases, endpoints, and call bound; no key required or network used.")
    sub.add_parser("list", help="List independent attacks and fixed task IDs.")
    return p


def plan_cases(args, p):
    scenarios = list(ATTACKS) if args.attack == "all" else [args.attack]
    cases = demo_workloads()
    if args.case_ids:
        unknown = set(args.case_ids) - {c["id"] for c in cases}
        if unknown: p.error("Unknown case IDs: " + ", ".join(sorted(unknown)))
        cases = [c for c in cases if c["id"] in args.case_ids]
    elif args.attack != "all":
        target = ATTACKS[args.attack].TARGET
        cases = [c for c in cases if c["kind"] == target]
    if not any(ATTACKS[s].TARGET == c["kind"] for c in cases for s in scenarios):
        p.error("The selected attack does not target any selected case.")
    # Failed prerequisite executions are retained and dependent reuse branches
    # are skipped; they are never silently replaced by new inference.
    count = len(cases)
    for c in cases:
        if c["kind"] == "study":
            count += int("request_injection" in scenarios)
        else:
            if "model_selection" in scenarios: count += 1
    return cases, scenarios, count


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    if args.command == "list":
        for name, module in ATTACKS.items(): print(f"{name:22} {module.TITLE}")
        print("\nFixed cases:")
        for c in demo_workloads(): print(f"{c['id']:22} {c['title']}")
        return 0
    if args.command == "run":
        if not 64 <= args.max_tokens <= 4096: p.error("--max-tokens must be 64..4096")
        if not 1 <= args.max_calls <= 100: p.error("--max-calls must be 1..100")
        if not 0 < args.timeout <= 300: p.error("--timeout must be >0 and <=300")
        cases, scenarios, upper = plan_cases(args, p)
        if upper > args.max_calls:
            p.error(f"Plan can attempt up to {upper} calls; increase --max-calls or choose fewer cases/attacks.")
        local = args.backend == "local"
        provider = args.provider or ("local" if local else "openrouter")
        try:
            models, policy = load_configuration(args.routing_config, provider, local, args.selector)
            routes = [{"case_id": c["id"], **select_question(c["prompt"], policy)} for c in cases]
            for route, case in zip(routes, cases):
                if "model_selection" in scenarios and case["kind"] == "quality" and not route["attack_model"]:
                    raise ValueError("Model-selection attack needs a model outside this question's eligible set.")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            p.error(f"Could not plan routing: {exc}")
        info = {"provider": provider, "selector": args.selector, "policy_hash": routes[0]["policy_hash"],
                "model_pool": [{"model": m["id"], "endpoint": m["base_url"],
                                "capabilities": m["capabilities"]} for m in models],
                "question_routes": routes,
                "max_call_bound": upper, "max_tokens_per_call": args.max_tokens,
                "case_ids": [c["id"] for c in cases], "scenarios": scenarios, "model_claim": args.model_claim}
        if args.dry_run:
            print(json.dumps(info, indent=2)); return 0
        settings = dict(local=local, max_tokens=args.max_tokens,
                        temperature=None if args.omit_temperature else args.temperature,
                        seed=args.seed, timeout=args.timeout)
        try:
            needed = {route["selected_model"] for route in routes}
            if "model_selection" in scenarios:
                needed.update(route["attack_model"] for route, case in zip(routes, cases) if case["kind"] == "quality")
            pool = {m["id"]: ChatBackend(m["base_url"], m["id"],
                           key_env=args.key_env or m["key_env"],
                           token_parameter=args.token_parameter or m["token_parameter"], **settings)
                    for m in models if m["id"] in needed}
            payload = run_experiment(cases, pool, scenarios, args.out,
                                          "live_local" if local else "live_api", info, args.model_claim,
                                          routing_policy=policy)
        except ValueError as exc: p.error(str(exc))
        print(terminal_report(payload, payload["runs"]))
        print(f"Trace: {args.out}")
        return 1 if payload["errors"] else 0
    try:
        payload = load(args.input)
        if payload.get("schema") != TRACE_SCHEMA:
            p.error("Use a recording generated by this attack demo.")
        records = score_records(payload)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        p.error(f"Could not read recording: {type(exc).__name__}")
    if args.command == "report":
        if args.out.exists(): p.error("Report output exists; choose a new path.")
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(markdown_report(payload, records), encoding="utf-8")
        print(args.out); return 0
    if args.attack != "all":
        attacked_cases = {r["case_id"] for r in records if r["scenario"] == args.attack}
        records = [r for r in records if r["scenario"] in ("honest", args.attack) and r["case_id"] in attacked_cases]
        if not records: p.error("This recording has no completed records for the selected attack.")
    print(terminal_report(payload, records, args.details))
    return 0


def attack_main(name):
    argv = sys.argv[1:]
    command = argv[0] if argv and argv[0] in {"run", "replay"} else "replay"
    rest = argv[1:] if argv and argv[0] in {"run", "replay"} else argv
    if "--attack" in rest:
        raise SystemExit("Each attack module selects its own scenario; omit --attack.")
    raise SystemExit(main([command, "--attack", name, *rest]))
