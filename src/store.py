"""持久化层：事件日志、幂等消息、核算版本、客户快照。

所有状态先写入 `.runtime/` 下的临时文件再原子替换；
版本提交顺序为「先版本文件、后当前指针」，进程在两者之间崩溃
只会留下一个未被引用的孤儿版本文件，不会出现半套结果。
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from util import GENESIS, chain_hash, hash_obj, now_iso, to_jsonable


class Store:
    def __init__(self, root: str | Path = ".runtime") -> None:
        self.root = Path(root)
        self.versions_dir = self.root / "versions"
        self.snapshots_dir = self.root / "snapshots"
        self.versions_dir.mkdir(parents=True, exist_ok=True)
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)
        self._events_path = self.root / "events.jsonl"
        self._messages_path = self.root / "messages.json"
        self._failed_path = self.root / "failed_rebuilds.jsonl"
        self._current_path = self.root / "current.json"
        self._events_path.touch(exist_ok=True)
        self._lock = threading.Lock()
        with self._events_path.open("r", encoding="utf-8") as fh:
            self._event_seq = sum(1 for line in fh if line.strip())

    # ------------------------------------------------------------ 事件日志

    def next_event_seq(self) -> int:
        with self._lock:
            self._event_seq += 1
            return self._event_seq

    def append_event(self, event: dict) -> None:
        line = json.dumps(to_jsonable(event), ensure_ascii=False)
        with self._events_path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def load_events(self) -> list[dict]:
        events = []
        with self._events_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    events.append(json.loads(line))
        return events

    def event_log_hash(self, events: list[dict] | None = None) -> str:
        """事件日志链式哈希：把版本与产生它的事件序列绑定。"""
        h = GENESIS
        for ev in events if events is not None else self.load_events():
            h = chain_hash(h, hash_obj(ev))
        return h

    # ------------------------------------------------------------ 幂等消息

    def _load_messages(self) -> dict:
        if not self._messages_path.exists():
            return {}
        with self._messages_path.open("r", encoding="utf-8") as fh:
            return json.load(fh)

    def get_message(self, message_id: str) -> dict | None:
        return self._load_messages().get(message_id)

    def save_message(self, message_id: str, record: dict) -> None:
        with self._lock:
            messages = self._load_messages()
            messages[message_id] = record
            self._atomic_write(self._messages_path, messages)

    # ------------------------------------------------------------ 核算版本

    def save_version(self, doc: dict) -> None:
        path = self.versions_dir / f"v{doc['version_seq']:06d}.json"
        self._atomic_write(path, doc)

    def set_current(self, version_seq: int) -> None:
        self._atomic_write(self._current_path, {"version_seq": version_seq,
                                                "updated_at": now_iso()})

    def current_seq(self) -> int | None:
        if not self._current_path.exists():
            return None
        with self._current_path.open("r", encoding="utf-8") as fh:
            return json.load(fh)["version_seq"]

    def load_version(self, version_seq: int) -> dict | None:
        path = self.versions_dir / f"v{version_seq:06d}.json"
        if not path.exists():
            return None
        with self._path_open(path) as fh:
            return json.load(fh)

    def list_versions(self) -> list[dict]:
        out = []
        for path in sorted(self.versions_dir.glob("v*.json")):
            with self._path_open(path) as fh:
                doc = json.load(fh)
            out.append({
                "version_seq": doc["version_seq"],
                "created_at": doc["created_at"],
                "trigger_message_ids": doc.get("trigger_message_ids", []),
                "events_included": doc.get("events_included"),
                "event_log_hash": doc.get("event_log_hash"),
                "supersedes": doc.get("supersedes"),
            })
        return out

    # ------------------------------------------------------------ 客户快照

    def save_snapshot(self, doc: dict) -> None:
        path = self.snapshots_dir / f"{doc['snapshot_id']}.json"
        if path.exists():
            raise FileExistsError(f"快照 {doc['snapshot_id']} 已存在，快照不可覆盖")
        self._atomic_write(path, doc)

    def get_snapshot(self, snapshot_id: str) -> dict | None:
        path = self.snapshots_dir / f"{snapshot_id}.json"
        if not path.exists():
            return None
        with self._path_open(path) as fh:
            return json.load(fh)

    def list_snapshots(self) -> list[dict]:
        out = []
        for path in sorted(self.snapshots_dir.glob("*.json")):
            with self._path_open(path) as fh:
                doc = json.load(fh)
            out.append({k: doc[k] for k in
                        ("snapshot_id", "account_id", "date", "version_seq", "issued_at", "label")})
        return out

    def next_snapshot_seq(self) -> int:
        return len(list(self.snapshots_dir.glob("*.json"))) + 1

    # ------------------------------------------------------------ 失败记录

    def append_failed(self, record: dict) -> None:
        line = json.dumps(to_jsonable(record), ensure_ascii=False)
        with self._failed_path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    # ------------------------------------------------------------ 内部

    @staticmethod
    def _path_open(path: Path):
        return path.open("r", encoding="utf-8")

    @staticmethod
    def _atomic_write(path: Path, obj: Any) -> None:
        tmp = path.with_name(path.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(to_jsonable(obj), fh, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
