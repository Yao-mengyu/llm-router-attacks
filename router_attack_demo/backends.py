"""Minimal local and HTTPS Chat Completions adapters. No secrets in traces."""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from .util import canonical


class BackendError(RuntimeError):
    pass


def reject_credential_echo(value, key):
    if key and key in json.dumps(value, ensure_ascii=False):
        raise BackendError("Backend response echoed a credential; response was not recorded.")


def token_usage(value):
    if not isinstance(value, dict):
        return None
    return {name: value[name] for name in ("prompt_tokens", "completion_tokens", "total_tokens")
            if type(value.get(name)) is int and value[name] >= 0}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise BackendError("Redirect refused; credentials are only sent to the configured endpoint.")


def validate_url(value: str, local: bool) -> str:
    try:
        p = urllib.parse.urlsplit(value)
        p.port
    except ValueError:
        raise ValueError("Endpoint has an invalid host or port.") from None
    if (not p.hostname or p.username or p.password or p.query or p.fragment
            or any(c.isspace() for c in value)):
        raise ValueError("Endpoint must have a host and no credentials, query, fragment, or whitespace.")
    if local:
        if p.scheme != "http" or p.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Local mode requires an HTTP loopback endpoint.")
    elif p.scheme != "https":
        raise ValueError("API mode requires HTTPS.")
    return value.rstrip("/")


@dataclass
class ChatBackend:
    base_url: str
    model: str
    local: bool = False
    key_env: str = "OPENAI_API_KEY"
    max_tokens: int = 384
    temperature: float | None = 0.0
    seed: int | None = None
    token_parameter: str = "max_completion_tokens"
    timeout: float = 90.0
    _api_key: str = field(default="", init=False, repr=False)

    def __post_init__(self):
        self.base_url = validate_url(self.base_url, self.local)
        if not isinstance(self.key_env, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.key_env):
            raise ValueError("key_env must name an environment variable.")
        if self.token_parameter not in {"max_tokens", "max_completion_tokens"}:
            raise ValueError("Unsupported token limit parameter.")
        if not self.local:
            self._api_key = os.environ.get(self.key_env, "")
            if not self._api_key.strip():
                raise ValueError(f"Set {self.key_env} in the environment before using API mode.")

    def generate(self, messages: list[dict]) -> dict:
        payload = {"model": self.model, "messages": messages, "stream": False,
                   self.token_parameter: self.max_tokens}
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        if self.seed is not None:
            payload["seed"] = self.seed
        headers = {"Content-Type": "application/json", "User-Agent": "Router-Attack-Demo/0.3"}
        if not self.local:
            headers["Authorization"] = "Bearer " + self._api_key
        req = urllib.request.Request(self.base_url + "/chat/completions",
                                     data=canonical(payload), headers=headers, method="POST")
        # Local inference never uses proxies. HTTPS uses ordinary system proxy
        # configuration; redirects are disabled in both modes.
        handlers = [NoRedirect()]
        if self.local:
            handlers.append(urllib.request.ProxyHandler({}))
        started = time.monotonic()
        try:
            with urllib.request.build_opener(*handlers).open(req, timeout=self.timeout) as response:
                data = response.read(4_000_001)
                if len(data) > 4_000_000:
                    raise BackendError("Backend response exceeded 4 MB.")
                raw = json.loads(data)
        except urllib.error.HTTPError as exc:
            # Providers can echo request contents/credentials in errors. Keep
            # only the status; neither bodies nor headers go into saved logs.
            status = exc.code
            exc.close()
            raise BackendError(f"Backend returned HTTP {status}; no response body logged.") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise BackendError("Backend connection failed or timed out; check endpoint and connectivity.") from None
        except (ValueError, UnicodeError):
            raise BackendError("Backend returned invalid JSON.") from None
        reject_credential_echo(raw, self._api_key)
        try:
            choice = raw["choices"][0]
            content = choice["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise ValueError()
        except (KeyError, IndexError, TypeError, ValueError):
            raise BackendError("Backend returned no text completion; trial retained as an error.") from None
        # Explicit allowlist instead of retaining arbitrary provider metadata.
        generation = {
            "text": content, "requested_model": self.model, "reported_model": raw.get("model"),
            "latency_seconds": time.monotonic() - started, "usage": token_usage(raw.get("usage")),
            "finish_reason": choice.get("finish_reason"),
            "request_settings": {k: v for k, v in payload.items() if k != "messages"},
        }
        # An exact snapshot/llama alias avoids confusing provider alias changes
        # with a malicious router. Use exact returned IDs for custom providers.
        if generation["reported_model"] != self.model:
            raise BackendError("Provider-reported model differs from configured model; use an exact snapshot/model ID.")
        return generation
