"""持久层：事件日志、核算版本、客户快照。

- events.jsonl 只追加，是系统唯一事实来源；
- 版本整体计算完成后先写临时文件再 os.replace 原子落盘，
  随后才原子推进 current 指针 —— 重算失败不会留下半套结果；
- 快照一旦发布即不可变，迟到事件只能产生新的核算版本。
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path


class StoreError(Exception):
    pass


def _atomic_write_json(path: Path, obj) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


class Store:
    def __init__(self, root: str | Path = ".runtime"):
        self.root = Path(root)
        self.versions_dir = self.root / "versions"
        self.snapshots_dir = self.root / "snapshots"
        for d in (self.root, self.versions_dir, self.snapshots_dir):
            d.mkdir(parents=True, exist_ok=True)
        self.events_file = self.root / "events.jsonl"
        self.messages_file = self.root / "messages.json"
        self.current_file = self.root / "current.json"
        self._lock = threading.Lock()
        if not self.messages_file.exists():
            _atomic_write_json(self.messages_file, {})

    # ---------------------------------------------------------------- events
    def append_events(self, envelopes: list[dict]) -> None:
        with self._lock, open(self.events_file, "a", encoding="utf-8") as fh:
            for env in envelopes:
                fh.write(json.dumps(env, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def load_events(self) -> list[dict]:
        if not self.events_file.exists():
            return []
        with open(self.events_file, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def next_seq(self) -> int:
        with self._lock:
            n = 0
            if self.events_file.exists():
                with open(self.events_file, encoding="utf-8") as fh:
                    n = sum(1 for line in fh if line.strip())
            return n

    # -------------------------------------------------------------- messages
    def load_messages(self) -> dict:
        with open(self.messages_file, encoding="utf-8") as fh:
            return json.load(fh)

    def save_messages(self, messages: dict) -> None:
        _atomic_write_json(self.messages_file, messages)

    # -------------------------------------------------------------- versions
    def save_version(self, version: dict) -> int:
        """原子落盘新版本并推进 current 指针。失败时 current 保持不变。"""
        with self._lock:
            cur = self.current()
            n = (cur["version"] if cur else 0) + 1
            version["version"] = n
            path = self.versions_dir / f"{n:06d}.json"
            _atomic_write_json(path, version)
            _atomic_write_json(self.current_file,
                               {"version": n, "version_hash": version["version_hash"]})
            return n

    def current(self) -> dict | None:
        if not self.current_file.exists():
            return None
        with open(self.current_file, encoding="utf-8") as fh:
            return json.load(fh)

    def load_version(self, n: int) -> dict:
        path = self.versions_dir / f"{n:06d}.json"
        if not path.exists():
            raise StoreError(f"版本不存在: {n}")
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    def list_versions(self) -> list[dict]:
        out = []
        for path in sorted(self.versions_dir.glob("*.json")):
            with open(path, encoding="utf-8") as fh:
                v = json.load(fh)
            out.append({"version": v["version"], "created_at": v["created_at"],
                        "version_hash": v["version_hash"],
                        "events_applied": len(v["events_applied"])})
        return out

    # ------------------------------------------------------------- snapshots
    def save_snapshot(self, snapshot: dict) -> None:
        path = self.snapshots_dir / f"{snapshot['snapshot_id']}.json"
        if path.exists():
            raise StoreError(f"快照已存在，不可覆盖: {snapshot['snapshot_id']}")
        _atomic_write_json(path, snapshot)

    def load_snapshot(self, snapshot_id: str) -> dict:
        path = self.snapshots_dir / f"{snapshot_id}.json"
        if not path.exists():
            raise StoreError(f"快照不存在: {snapshot_id}")
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    def list_snapshots(self) -> list[dict]:
        out = []
        for path in sorted(self.snapshots_dir.glob("*.json")):
            with open(path, encoding="utf-8") as fh:
                s = json.load(fh)
            out.append({k: s[k] for k in ("snapshot_id", "account", "date",
                                          "version", "snapshot_hash", "published_at")})
        return out
