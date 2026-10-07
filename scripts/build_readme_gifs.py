#!/usr/bin/env python3
"""Render three looping README GIFs from recorded API traces.

This draws diagram frames directly from code. Timeline timing is illustrative;
the model outputs and attack mutations come from the saved real recording.
Pillow is an optional authoring dependency, not a demo runtime dependency.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from router_attack_demo.util import AD_SENTENCE
from router_attack_demo.report import score_records

WIDTH, HEIGHT = 1200, 700
C = {"bg": "#0c1526", "panel": "#18263c", "edge": "#354763", "white": "#edf4ff",
     "muted": "#b0c1d8", "blue": "#80b9ff", "green": "#8ce3bb", "red": "#ff9dac",
     "good_bg": "#153031", "bad_bg": "#352333"}
STEPS = ("CLIENT REQUEST", "ROUTER ACTION", "MODEL GENERATION", "CLIENT DELIVERY")


def find_font(bold=False):
    name = "Arial Bold.ttf" if bold else "Arial.ttf"
    candidates = [Path("/System/Library/Fonts/Supplemental") / name,
                  Path("/usr/share/fonts/truetype/dejavu") / ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"),
                  Path("/usr/share/fonts/truetype/liberation2") / ("LiberationSans-Bold.ttf" if bold else "LiberationSans-Regular.ttf")]
    return next((p for p in candidates if p.exists()), None)


FONT_CACHE = {}


def font(size, bold=False):
    key = (size, bold)
    if key not in FONT_CACHE:
        path = find_font(bold)
        FONT_CACHE[key] = ImageFont.truetype(str(path), size) if path else ImageFont.load_default(size=size)
    return FONT_CACHE[key]


def text(draw, x, y, value, size=22, color="white", bold=False):
    draw.text((x, y), str(value), font=font(size, bold), fill=C.get(color, color))


def wrap(draw, value, width, size=22):
    lines = []
    for paragraph in str(value).splitlines() or [""]:
        current = ""
        for word in paragraph.split():
            candidate = (current + " " + word).strip()
            if draw.textlength(candidate, font=font(size)) <= width:
                current = candidate
            else:
                if current: lines.append(current)
                current = word
        lines.append(current)
    return lines


def paragraph(draw, x, y, value, width, size=22, color="white", max_lines=4, gap=7):
    lines = wrap(draw, value, width, size)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        while draw.textlength(lines[-1] + "...", font=font(size)) > width:
            lines[-1] = lines[-1][:-1]
        lines[-1] += "..."
    for i, line in enumerate(lines):
        text(draw, x, y + i * (size + gap), line, size, color)


def box(draw, bounds, fill="panel", outline="edge", width=1, radius=14):
    draw.rounded_rectangle(bounds, radius=radius, fill=C.get(fill, fill), outline=C.get(outline, outline), width=width)


def arrow(draw, start, end, color, progress=None):
    draw.line((start, end), fill=C[color], width=3)
    dx = 1 if end[0] > start[0] else -1
    x, y = end
    draw.polygon([(x, y), (x - dx * 9, y - 6), (x - dx * 9, y + 6)], fill=C[color])
    if progress is not None:
        px = start[0] + (end[0] - start[0]) * progress
        py = start[1] + (end[1] - start[1]) * progress
        draw.ellipse((px - 6, py - 6, px + 6, py + 6), fill=C[color])


def short_model(value):
    names = {"anthropic/claude-opus-4.6": "Claude Opus 4.6",
             "meta-llama/llama-3.2-3b-instruct": "Llama 3.2 3B"}
    return names.get(value, value.split("/")[-1])


def visible_answer(record):
    """Display returned IDs without relaxing the exact JSON task contract."""
    raw = record["backend_response"].strip()
    if raw.startswith("```") and raw.endswith("```"):
        raw = "\n".join(raw.splitlines()[1:-1])
    try:
        value = json.loads(raw)
    except ValueError:
        return record["evaluation"]["answer"]
    return value.get("answer") if isinstance(value, dict) else value


def render(name, honest, attack, stage, progress=1.0):
    image = Image.new("RGB", (WIDTH, HEIGHT), C["bg"])
    d = ImageDraw.Draw(image)
    config = {
        "request_injection": ("01", "Request modification", "The router adds an advertisement requirement before inference."),
        "response_tampering": ("02", "Response modification", "The router appends an advertisement after the model generates its answer."),
        "model_selection": ("03", "Model selection", "Eligible models depend on the question; the router substitutes an ineligible backend."),
    }
    number, title, subtitle = config[name]
    text(d, 32, 24, "LLM ROUTER / " + number + " / RECORDED API WALKTHROUGH", 17, "blue", True)
    text(d, 32, 58, title, 38, "white", True)
    text(d, 32, 112, subtitle, 21, "muted")
    tab_width = 275
    for i, label in enumerate(STEPS):
        x = 32 + i * 287
        active = stage == i
        box(d, (x, 155, x + tab_width, 195), "panel", "blue" if active else "edge", 2 if active else 1, 9)
        text(d, x + 12, 166, f"{i + 1}  {label}", 15, "blue" if active else "muted", active)

    is_model = name == "model_selection"
    client_line = ("Access-policy question\nEligible: " + short_model(honest["actual_model"])) if is_model else "Study advice. No products or paid services."
    if is_model:
        router_line = "Claims " + short_model(attack["returned_model"])
        backend_line = short_model(attack["actual_model"]) if stage >= 1 else short_model(honest["actual_model"])
    elif name == "request_injection":
        router_line = "Adds hidden sponsor instruction" if stage >= 1 else "Receives the client's request"
        backend_line = "Receives modified request" if stage >= 2 else "Honest backend"
    else:
        router_line = "Adds sponsor text to delivery" if stage >= 3 else "Forwards the unchanged request"
        backend_line = "Returns the original answer" if stage >= 2 else "Honest backend"
    actors = [(32, "CLIENT / ROUTING POLICY" if is_model else "CLIENT", client_line),
              (436, "UNTRUSTED ROUTER", router_line), (840, "MODEL BACKEND", backend_line)]
    actor_active = (0, 1, 2, 0)[stage]
    for i, (x, label, detail) in enumerate(actors):
        box(d, (x, 222, x + 328, 319), "panel", "blue" if actor_active == i else "edge", 2)
        text(d, x + 16, 235, label, 16, "blue", True)
        paragraph(d, x + 16, 264, detail, 297, 21, max_lines=2, gap=4)
    arrow(d, (364, 270), (428, 270), "red" if name == "request_injection" and stage >= 1 else "blue",
          progress if stage == 1 else None)
    arrow(d, (768, 270), (832, 270), "red" if name in ("request_injection", "model_selection") and stage >= 2 else "blue",
          progress if stage == 2 else None)
    if stage >= 3:
        arrow(d, (998, 334), (195, 334), "red", progress if stage == 3 else None)
        d.rectangle((463, 323, 609, 344), fill=C["bg"])
        text(d, 473, 325, "delivered answer", 14, "red")

    box(d, (32, 356, 582, 592), "good_bg", "green")
    box(d, (606, 356, 1168, 592), "bad_bg", "red")
    text(d, 54, 374, "NORMAL ROUTE", 18, "green", True)
    text(d, 628, 374, "ATTACK ROUTE", 18, "red", True)
    if is_model:
        text(d, 54, 412, "Correct allowed IDs", 19, "muted")
        text(d, 54, 443, json.dumps(honest["evaluation"]["answer"]), 30, "white", True)
        text(d, 54, 495, "Correct account decisions", 21, "green", True)
        text(d, 54, 536, "Serving / claimed: " + short_model(honest["actual_model"]), 20, "muted")
        if stage < 2:
            text(d, 628, 412, "Router selects an unauthorized backend", 20, "muted")
            text(d, 628, 449, "Claimed: " + short_model(attack["returned_model"]), 25, "white", True)
            text(d, 628, 495, "Actual: " + short_model(attack["actual_model"]), 25, "red", True)
        else:
            text(d, 628, 412, "Returned allowed IDs", 19, "muted")
            returned = visible_answer(attack)
            text(d, 628, 443, json.dumps(returned), 30, "white", True)
            extra = [item for item in (returned or []) if item not in attack["evaluation"]["gold"]]
            missing = [item for item in attack["evaluation"]["gold"] if item not in (returned or [])]
            effect = "wrongly allows " + ", ".join(extra) if extra else "missing " + ", ".join(missing)
            text(d, 628, 495, "Incorrect result / " + effect, 21, "red", True)
            text(d, 628, 536, "Actual model: " + short_model(attack["actual_model"]), 20, "muted")
    else:
        text(d, 54, 412, "Raw backend answer (excerpt)", 19, "muted")
        excerpt = honest["backend_response"].split("\n")[0].replace("**", "")
        paragraph(d, 54, 445, excerpt, 500, 22, max_lines=3)
        text(d, 54, 552, "No advertisement in the answer", 21, "green", True)
        if name == "request_injection":
            label = "Inserted system instruction" if stage < 2 else "Raw model / delivered answer (excerpt)"
            text(d, 628, 412, label, 19, "muted")
            paragraph(d, 628, 445, AD_SENTENCE if stage >= 1 else "The original user request excludes product recommendations.",
                      510, 22, max_lines=3)
            text(d, 628, 552, "Sponsor generated by the model" if stage >= 2 else "Unauthorized request addition", 21, "red", True)
        else:
            label = "Delivered answer (appended excerpt)" if stage >= 3 else "Backend response is unchanged"
            text(d, 628, 412, label, 19, "muted")
            paragraph(d, 628, 445, AD_SENTENCE if stage >= 3 else "The model generates study advice without the fictional sponsor.",
                      510, 22, max_lines=3)
            text(d, 628, 552, "Sponsor added by the router" if stage >= 3 else "No sponsor in the model's answer", 21, "red", True)

    captions = {
        "request_injection": (
            "The client asks for study advice without product recommendations.",
            "The router adds a sponsor instruction the client did not authorize.",
            "The model generates sponsor text in its original answer.",
            "Impact: the client receives an advertisement generated under the modified request.",
        ),
        "response_tampering": (
            "The client asks for study advice without product recommendations.",
            "The router forwards the original request to the model.",
            "The model returns study advice without sponsor text.",
            "Impact: the router appends an advertisement the model never generated.",
        ),
        "model_selection": (
            "The routing policy selects eligible models for the access-policy question.",
            "The router substitutes an ineligible model while claiming the expected identity.",
            "The substitute model returns incorrect IDs and breaks the required JSON format.",
            "Impact: incorrect account decisions reach the client under the expected model label.",
        ),
    }
    box(d, (32, 612, 1168, 654), "panel", "red" if stage == 3 else "edge", 2, 10)
    text(d, 50, 623, captions[name][stage], 20, "red" if stage == 3 else "muted", stage == 3)
    text(d, 32, 673, "Real recorded API outputs. Timeline illustrates message flow; durations are not measured latency.", 16, "muted")
    return image


def build(source):
    payload = json.loads(source.read_text())
    records = score_records(payload)
    manifest = {"source": str(source.relative_to(ROOT)), "mode": payload["provenance"]["mode"],
                "note": "Animations show router actions and attack impact using recorded API data. Durations illustrate sequence, not measured latency.", "animations": []}
    destination = ROOT / "docs/assets"
    destination.mkdir(exist_ok=True)
    for name in ("request_injection", "response_tampering", "model_selection"):
        case_id = "access-policy" if name == "model_selection" else "study-no-products"
        honest = next(r for r in records if r["scenario"] == "honest" and r["case_id"] == case_id)
        attack = next(r for r in records if r["scenario"] == name and r["case_id"] == case_id)
        if name == "model_selection":
            assert honest["evaluation"]["task_pass"] and not attack["evaluation"]["task_pass"]
        else:
            assert not honest["evaluation"]["sponsor_present"] and attack["evaluation"]["sponsor_present"]
            assert AD_SENTENCE in attack["delivered_response"]
        frames, durations = [], []
        for stage in range(len(STEPS)):
            if stage in (1, 2, 3):
                for step in range(8):
                    frames.append(render(name, honest, attack, stage, step/7))
                    durations.append(80)
            frames.append(render(name, honest, attack, stage))
            durations.append(3200 if stage == len(STEPS) - 1 else 1800)
        out = destination / (name + ".gif")
        palette = frames[-1].quantize(colors=128, method=Image.Quantize.MEDIANCUT)
        indexed = [frame.quantize(palette=palette, dither=Image.Dither.NONE) for frame in frames]
        indexed[0].save(out, save_all=True, append_images=indexed[1:], duration=durations,
                        loop=0, disposal=1, optimize=True)
        with Image.open(out) as gif:
            stored_duration = 0
            for frame in range(gif.n_frames):
                gif.seek(frame)
                stored_duration += gif.info["duration"]
            assert gif.n_frames > 1 and gif.info["loop"] == 0
            manifest["animations"].append({"scenario": name, "source_record": attack["id"],
                "file": out.name, "frames": gif.n_frames, "duration_ms": stored_duration,
                "bytes": out.stat().st_size, "stages": list(STEPS),
                "impact": {"request_injection": "Sponsor text generated under an unauthorized instruction.",
                           "response_tampering": "Sponsor text appended by the router after inference.",
                           "model_selection": "Incorrect account decisions delivered under a false model label."}[name]})
        print(f"{out.name}: {out.stat().st_size:,} bytes, {stored_duration/1000:g}s loop")
    (destination / "animations.json").write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=ROOT / "examples/api/trace.json")
    build(parser.parse_args().input.resolve())
