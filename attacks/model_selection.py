"""Serve an ineligible model, optionally claiming the approved model's identity."""
NAME = "model_selection"
TITLE = "Model selection / substitution"
TARGET = "quality"


def mutate(plan, case):
    plan["backend"] = "alternate"
    plan["effect"] = "Serve an unauthorized model; compare the task result and the router's returned model claim."


if __name__ == "__main__":
    from router_attack_demo.cli import attack_main
    attack_main(NAME)
