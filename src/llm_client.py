"""Open-source LLM client — OpenAI-compatible /chat/completions.

Written against the OpenAI-compatible interface on purpose, because both viable
local backends speak it:

    Ollama :  ollama serve            -> http://localhost:11434/v1
    vLLM   :  vllm serve <model>      -> http://localhost:8000/v1

Only `requests` is needed; no vendor SDK. Set llm.base_url and llm.model in
config.yaml to switch backends with no code change.
"""

from __future__ import annotations

import json
import time
from typing import Any

import requests


class LLMError(RuntimeError):
    pass


class LLMClient:
    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str = "not-needed-for-local",
        temperature: float = 0.2,
        max_tokens: int = 900,
        force_json: bool = True,
        timeout_s: float = 20.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.force_json = force_json
        self.timeout_s = timeout_s

    @classmethod
    def from_config(cls, cfg) -> "LLMClient":
        llm = cfg.get_path("llm", {})
        return cls(
            base_url=llm.get("base_url", "http://localhost:11434/v1"),
            model=llm.get("model", "llama3.1:8b"),
            api_key=llm.get("api_key", "not-needed-for-local"),
            temperature=float(llm.get("temperature", 0.2)),
            max_tokens=int(llm.get("max_tokens", 900)),
            force_json=bool(llm.get("force_json", True)),
            timeout_s=float(cfg.get_path("agent.timeout_s", 20)),
        )

    # ---------------------------------------------------------------- health

    def health(self) -> dict[str, Any]:
        """Cheap pre-flight check. Run this in Phase 0 before wiring anything."""
        try:
            r = requests.get(f"{self.base_url}/models", timeout=5)
            r.raise_for_status()
            data = r.json()
            names = [m.get("id") for m in data.get("data", [])]
            return {"ok": True, "models": names, "configured_model": self.model,
                    "model_available": self.model in names if names else "unknown"}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "base_url": self.base_url}

    # ------------------------------------------------------------- completion

    def complete_json(self, system: str, user: str) -> tuple[dict[str, Any], dict[str, Any]]:
        """Return (parsed_json, meta). Raises LLMError on transport failure.

        JSON mode is the single biggest reliability win with 7-8B models. Both
        Ollama and vLLM honour response_format={"type": "json_object"}; if the
        backend ignores it we fall back to bracket extraction.
        """
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": False,
        }
        if self.force_json:
            payload["response_format"] = {"type": "json_object"}

        t0 = time.time()
        try:
            r = requests.post(
                f"{self.base_url}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {self.api_key}",
                         "Content-Type": "application/json"},
                timeout=self.timeout_s,
            )
            r.raise_for_status()
            body = r.json()
        except requests.Timeout as exc:
            raise LLMError(f"LLM timeout after {self.timeout_s}s") from exc
        except Exception as exc:
            raise LLMError(f"LLM request failed: {exc}") from exc

        latency_ms = int((time.time() - t0) * 1000)
        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as exc:
            raise LLMError(f"unexpected response shape: {str(body)[:300]}") from exc

        usage = body.get("usage", {}) or {}
        meta = {
            "latency_ms": latency_ms,
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "model": body.get("model", self.model),
            "raw_chars": len(content),
        }
        return extract_json(content), meta


def extract_json(text: str) -> dict[str, Any]:
    """Parse JSON from a model response, tolerating fences and prose.

    Order matters: try the whole string first (JSON mode gives us clean output),
    then strip markdown fences, then take the outermost brace-balanced span.
    """
    text = (text or "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    if "```" in text:
        for block in text.split("```")[1::2]:
            block = block.lstrip()
            if block.lower().startswith("json"):
                block = block[4:]
            try:
                return json.loads(block.strip())
            except json.JSONDecodeError:
                continue

    start = text.find("{")
    if start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start : i + 1])
                    except json.JSONDecodeError:
                        break

    raise LLMError(f"no parseable JSON in response: {text[:200]!r}")
