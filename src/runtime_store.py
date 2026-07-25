"""File-backed shared state between the simulation process and the MCP server.

Why a file and not shared memory: the MCP server has to be a separate process
(that is what MCP is), but the simulation must never block on it. A snapshot file
written atomically gives us a decoupled, crash-safe boundary — if the MCP server
dies, the simulation does not notice.

Layout under results/store/:
    snapshot.json          latest simulation state (overwritten atomically)
    policy_inbox.jsonl     policies proposed by the agent (append-only)
    policy_history.jsonl   policies actually installed (append-only)
    agent_trace.jsonl      every LLM call, tool call, rejection and retry
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any


class RuntimeStore:
    def __init__(self, store_dir: str | Path):
        self.dir = Path(store_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.snapshot_path = self.dir / "snapshot.json"
        self.inbox_path = self.dir / "policy_inbox.jsonl"
        self.history_path = self.dir / "policy_history.jsonl"
        self.trace_path = self.dir / "agent_trace.jsonl"
        self._lock = threading.Lock()

    # -------------------------------------------------------------- atomic write

    def _atomic_write(self, path: Path, text: str) -> None:
        """Write via temp file + os.replace so a reader never sees a partial file."""
        fd, tmp = tempfile.mkstemp(dir=str(self.dir), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _append(self, path: Path, record: dict[str, Any]) -> None:
        record.setdefault("wall_time", time.time())
        line = json.dumps(record, default=str) + "\n"
        with self._lock:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line)

    # ----------------------------------------------------------------- snapshot

    def write_snapshot(self, payload: dict[str, Any]) -> None:
        payload = dict(payload)
        payload["wall_time"] = time.time()
        try:
            self._atomic_write(self.snapshot_path, json.dumps(payload, default=str, indent=2))
        except Exception:
            # A failed snapshot write must never take down the simulation.
            pass

    def read_snapshot(self) -> dict[str, Any]:
        try:
            with open(self.snapshot_path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError):
            return {"status": "no_snapshot"}

    # ------------------------------------------------------------------ policies

    def propose_policy(self, policy: dict[str, Any], source: str = "mcp") -> None:
        self._append(self.inbox_path, {"source": source, "policy": policy})

    def drain_inbox(self) -> list[dict[str, Any]]:
        """Read and clear the inbox. Called by the sim process."""
        with self._lock:
            if not self.inbox_path.exists():
                return []
            try:
                lines = self.inbox_path.read_text(encoding="utf-8").splitlines()
                self.inbox_path.write_text("", encoding="utf-8")
            except OSError:
                return []
        out = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def record_installed_policy(self, policy: dict[str, Any], sim_hour: float, source: str) -> None:
        self._append(self.history_path, {"sim_hour": sim_hour, "source": source, "policy": policy})

    # --------------------------------------------------------------------- trace

    def trace(self, event: str, **fields: Any) -> None:
        """Append to agent_trace.jsonl.

        This file is the evidence for the 'Agentic Autonomy' criterion — every
        tool call, rejection and self-correction lands here. Show it in the deck.
        """
        self._append(self.trace_path, {"event": event, **fields})

    def read_trace(self, tail: int = 50) -> list[dict[str, Any]]:
        try:
            lines = self.trace_path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        out = []
        for line in lines[-tail:]:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def reset(self) -> None:
        for p in (self.snapshot_path, self.inbox_path, self.history_path, self.trace_path):
            try:
                p.unlink()
            except FileNotFoundError:
                pass
