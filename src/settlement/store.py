"""JSONL 事件日志持久化，写入 .runtime/。"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from .engine import SettlementEngine


class EventStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._flock = threading.Lock()
        self.engine = self._load()

    def _load(self) -> SettlementEngine:
        if not self.path.exists():
            return SettlementEngine()
        records = []
        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
        return SettlementEngine.replay(records)

    def handle(self, command: dict):
        with self._flock:
            stored = self.engine.handle(command)
            self._append(stored)
            return stored

    def sweep(self, at=None):
        with self._flock:
            stored = self.engine.sweep_expired(at)
            self._append(stored)
            return stored

    def _append(self, stored) -> None:
        if not stored:
            return
        seqs = {e.seq for e in stored}
        records = [e for e in self.engine.state.events if e.seq in seqs]
        with self.path.open("a", encoding="utf-8") as f:
            for e in records:
                f.write(
                    json.dumps(
                        {
                            "seq": e.seq,
                            "type": e.type,
                            "occurred_at": e.occurred_at.isoformat(),
                            "received_at": e.received_at.isoformat(),
                            "payload": e.payload,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
            f.flush()
