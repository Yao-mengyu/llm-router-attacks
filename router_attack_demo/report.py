"""Render attack actions and observed impact as terminal text and Markdown."""
from __future__ import annotations

import json

from .workloads import evaluate

SCENARIO_LABELS = {
    "honest": "Normal route", "model_selection": "Model substitution",
    "request_injection": "Request modification with an advertisement",
    "response_tampering": "Response modification with an advertisement",
}


def score_records(payload: dict) -> list[dict]:
    """Describe outcomes from the recorded text and fixed task answers."""
    cases = {case['id']: case for case in payload['cases']}
    records = []
    for saved in payload['runs']:
        record = dict(saved)
        case = cases[record['case_id']]
        record['evaluation'] = evaluate(case, record['delivered_response'])
        record['backend_evaluation'] = evaluate(case, record['backend_response'])
        records.append(record)
    return records


def quality_pairs(records):
    honest = {r["case_id"]: r for r in records if r["scenario"] == "honest" and r["case_kind"] == "quality"}
    return [(honest[r["case_id"]], r) for r in records
            if r["scenario"] == "model_selection" and r["case_id"] in honest]


def terminal_report(payload, records, details=False):
    lines = [f"LLM router attack demo | {payload['provenance']['mode']}", ""]
    routes = {r["case_id"]: r["routing_decision"] for r in records if "routing_decision" in r}
    for case_id, route in routes.items():
        lines.append(f"Route {case_id}: selector={route.get('selector', 'rules')}; category={route['rule_id']}; eligible={route['eligible_models']}; selected={route['selected_model']}.")
    for r in records:
        e = r["evaluation"]
        impact = (f"result={'CORRECT' if e['task_pass'] else 'INCORRECT'}; answer={e['answer']!r}; expected={e['gold']!r}"
                  if r["case_kind"] == "quality" else f"sponsor={'YES' if e['sponsor_present'] else 'NO'}")
        if r["case_kind"] == "quality" and not e["format_ok"]:
            impact += "; format=INVALID; raw_output=" + repr(r["delivered_response"])
        lines.append(f"{r['id']}: {impact} | claimed={r['returned_model']}; actual={r['actual_model']}")
        if details:
            lines += [f"  router claim: {r['returned_model']}", f"  serving:      {r['actual_model']}",
                      "  client request: " + json.dumps(r["client_messages"], ensure_ascii=False),
                      "  backend input:  " + json.dumps(r["backend_messages"], ensure_ascii=False),
                      "  backend:    " + r["backend_response"].replace("\n", "\n              "),
                      "  delivered:  " + r["delivered_response"].replace("\n", "\n              ")]
    pairs = quality_pairs(records)
    if pairs:
        good = sum(a["evaluation"]["task_pass"] for a, b in pairs)
        alt = sum(b["evaluation"]["task_pass"] for a, b in pairs)
        regressions = sum(a["evaluation"]["task_pass"] and not b["evaluation"]["task_pass"] for a, b in pairs)
        lines += ["", f"Correct task outputs: normal={good}/{len(pairs)}, substituted={alt}/{len(pairs)}; regressions={regressions}."]
    lines += [f"Model calls attempted: {payload['model_calls_attempted']}; retained errors: {len(payload['errors'])}."]
    return "\n".join(lines) + "\n"


def fence(text, language="text"):
    # A model can output Markdown fences. Choose a longer delimiter so the
    # model's raw output cannot escape the report's code block.
    import re
    length = max([3] + [len(m.group()) + 1 for m in re.finditer(r"`{3,}", text)])
    delimiter = "`" * length
    return f"{delimiter}{language}\n{text}\n{delimiter}"


def cell(value):
    return str(value).replace("|", "\\|").replace("\n", " ").replace("<", "&lt;").replace(">", "&gt;")


def markdown_report(payload, records):
    p = payload["provenance"]
    lines = ["# Recorded attack demo", "", f"Source mode: **{p['mode']}**. Generated: `{p['generated_at']}`.", "",
             "Each example shows the client request, the model's actual input and output, and the answer delivered by the router.", ""]
    routes = {r["case_id"]: (r["case_title"], r["routing_decision"]) for r in records if "routing_decision" in r}
    if routes:
        lines += ["## Question-based routing", "",
                  "Routing is evaluated on the original client question before inference. Lower preference values win within the eligible set.", "",
                  "| Question | Routing category | Eligible models | Selected model |", "|---|---|---|---|"]
        for title, route in routes.values():
            lines.append(f"| {cell(title)} | {cell(route['rule_id'])} | {cell(', '.join(route['eligible_models']))} | {cell(route['selected_model'])} |")
        lines += ["", "## Attack outcomes", ""]
    lines += ["| Scenario | Recorded branches | Sponsor in model output | Sponsor in delivered answer |",
              "|---|---:|---:|---:|"]
    for name, label in SCENARIO_LABELS.items():
        subset = [r for r in records if r["scenario"] == name]
        if not subset: continue
        sponsor_raw = sum(r["backend_evaluation"].get("sponsor_present", False) for r in subset)
        sponsor_delivered = sum(r["evaluation"].get("sponsor_present", False) for r in subset)
        lines.append(f"| {label} | {len(subset)} | {sponsor_raw} | {sponsor_delivered} |")
    pairs = quality_pairs(records)
    if pairs:
        lines += ["", "## Model selection: task impact", "",
                  "The routing policy chooses eligible models for each question.",
                  "Both models receive identical task messages. The attack switches the model and may falsely label its identity.",
                  "The primary outcome is exact answer **and** the one-key JSON contract. Every paired case is shown.", "",
                  "| Fixed task | Expected answer | Normal output | Substituted-model output | Normal / substituted result |",
                  "|---|---|---|---|---|"]
        for a, b in pairs:
            ea, eb = a["evaluation"], b["evaluation"]
            lines.append(f"| {cell(a['case_title'])} | {cell(json.dumps(ea['gold']))} | "
                         f"{cell(a['delivered_response'])} | {cell(b['delivered_response'])} | "
                         f"{'CORRECT' if ea['task_pass'] else 'INCORRECT'} / {'CORRECT' if eb['task_pass'] else 'INCORRECT'} |")
        good = sum(a["evaluation"]["task_pass"] for a, b in pairs)
        alt = sum(b["evaluation"]["task_pass"] for a, b in pairs)
        lines += ["", f"Paired task passes: **{good}/{len(pairs)} approved vs. {alt}/{len(pairs)} unauthorized**."]
    lines += ["", "## Expand the exact traces", ""]
    for r in records:
        title = cell(r["id"])
        lines += [f"<details><summary>{title}</summary>", "",
                  f"Router's returned model claim: `{cell(r['returned_model'])}`. Actual serving model: `{cell(r['actual_model'])}`.",
                  f"Eligible models for this question: `{cell(', '.join(r.get('eligible_models', [])))}`.",
                  "",
                  "Client request:", "", fence(json.dumps(r["client_messages"], ensure_ascii=False, indent=2), "json"), "",
                  "Request actually received by the backend:", "",
                  fence(json.dumps(r["backend_messages"], ensure_ascii=False, indent=2), "json"), "",
                  "Raw backend response:", "", fence(r["backend_response"]), "",
                  "Delivered response:", "", fence(r["delivered_response"]), "",
                  f"Reused execution: `{cell(r.get('reused_execution') or 'none (new generation)')}`.", "",
                  "</details>", ""]
    lines += [f"Model calls attempted: **{payload['model_calls_attempted']}**. Retained errors: **{len(payload['errors'])}**."]
    if payload["errors"]:
        lines += ["", "Retained errors:", "", fence(json.dumps(payload["errors"], indent=2), "json")]
    return "\n".join(lines) + "\n"
