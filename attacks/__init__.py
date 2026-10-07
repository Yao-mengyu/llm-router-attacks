"""Each router deviation lives in an independently runnable module."""

from collections.abc import Mapping
from importlib import import_module


class AttackModules(Mapping):
    """Lazy imports keep `python -m attacks.<name>` free of runpy warnings."""
    names = ("model_selection", "request_injection", "response_tampering")

    def __getitem__(self, name):
        if name not in self.names:
            raise KeyError(name)
        return import_module("attacks." + name)

    def __iter__(self): return iter(self.names)
    def __len__(self): return len(self.names)


ATTACKS = AttackModules()
