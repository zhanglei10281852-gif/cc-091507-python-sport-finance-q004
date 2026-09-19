"""JSON 文件持久化。

整个引擎状态保存为单个 JSON 文档，写入采用 临时文件 + os.replace 保证原子性。
所有集合都是 append-only 列表，分录、事件、发票版本永不原地修改，
因此任意历史时点的预算快照都可以从记录中重建。
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

COLLECTIONS = (
    "contracts",
    "budgets",
    "invoices",
    "payments",
    "events",
    "entries",
    "approvals",
)


def empty_state() -> dict[str, Any]:
    state: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "counters": {}}
    for name in COLLECTIONS:
        state[name] = []
    return state


class Store:
    """带锁的 JSON 文档存储。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._state = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return empty_state()
        with self.path.open("r", encoding="utf-8") as fh:
            state = json.load(fh)
        for name in COLLECTIONS:
            state.setdefault(name, [])
        state.setdefault("counters", {})
        return state

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(self._state, fh, ensure_ascii=False, indent=1)
                fh.write("\n")
            os.replace(tmp, self.path)

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def collection(self, name: str) -> list[dict[str, Any]]:
        return self._state[name]

    def next_id(self, prefix: str) -> str:
        """单调递增的稳定标识，如 inv-0007。"""
        with self._lock:
            counters = self._state["counters"]
            counters[prefix] = counters.get(prefix, 0) + 1
            return f"{prefix}-{counters[prefix]:04d}"

    def append(self, name: str, record: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self.collection(name).append(record)
            return record
