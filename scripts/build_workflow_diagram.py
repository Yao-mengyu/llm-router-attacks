#!/usr/bin/env python3
"""Render the README workflow as a PNG without a Markdown diagram plugin."""
from pathlib import Path

from PIL import Image, ImageDraw

from build_readme_gifs import C, arrow, box, font, text

ROOT = Path(__file__).resolve().parents[1]


def centered(draw, x, y, value, size=22, color="white", bold=False):
    width = draw.textlength(value, font=font(size, bold))
    text(draw, x - width / 2, y, value, size, color, bold)


def render():
    canvas = Image.new("RGB", (1200, 840), C["bg"])
    draw = ImageDraw.Draw(canvas)
    text(draw, 32, 28, "NORMAL REQUEST / RESPONSE PATH", 17, "blue", True)
    text(draw, 32, 64, "LLM router workflow", 38, bold=True)
    text(draw, 32, 119, "Question-based model selection, request forwarding, and response forwarding.", 22, "muted")

    actors = (
        (185, "Client application", "Question and constraints"),
        (600, "LLM router", "Policy and forwarding"),
        (1015, "Selected backend", "Model inference"),
    )
    for center, title, detail in actors:
        box(draw, (center - 153, 175, center + 153, 247), outline="blue")
        centered(draw, center, 188, title, 24, bold=True)
        centered(draw, center, 221, detail, 16, "muted")
        for y in range(251, 759, 14):
            draw.line((center, y, center, y + 7), fill=C["edge"], width=2)

    arrow(draw, (185, 300), (600, 300), "blue")
    centered(draw, 390, 267, "Client request", 23)

    box(draw, (425, 324, 775, 460), outline="blue", width=2)
    centered(draw, 600, 339, "1. MODEL SELECTION", 17, "blue", True)
    centered(draw, 600, 373, "Question + routing policy", 22)
    centered(draw, 600, 404, "Identify eligible models", 22)
    centered(draw, 600, 435, "Choose a backend", 20, "muted")

    arrow(draw, (600, 520), (1015, 520), "blue")
    centered(draw, 808, 483, "2. Forward request", 23)
    centered(draw, 808, 539, "Authorized preparation and formatting", 17, "muted")

    arrow(draw, (1015, 625), (600, 625), "green")
    centered(draw, 808, 591, "Backend response", 23)

    arrow(draw, (600, 720), (185, 720), "green")
    centered(draw, 391, 685, "3. Forward response", 23)
    centered(draw, 391, 739, "Preserve answer and model identity", 17, "muted")

    text(draw, 32, 795, "Eligible models depend on the question; the router also controls both forwarding steps.", 19, "muted")
    return canvas


if __name__ == "__main__":
    destination = ROOT / "docs/assets/router_workflow.png"
    destination.parent.mkdir(parents=True, exist_ok=True)
    render().save(destination, optimize=True)
    print(destination)
