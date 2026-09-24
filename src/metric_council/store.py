"""原子化 JSON 持久化：所有收件/审议/修订状态的唯一事实来源。

进程重启后用同一路径重建服务，即可凭已落盘状态保证幂等——不会重复发起
会审、不会重复发布回算任务。每次状态变更走“临时文件 + ``os.replace``”
原子落盘，单进程内由锁串行化，批量处理中途崩溃不会留下半写文件。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any


class JsonStore:
    """以一个 JSON 文件承载全部命名状态段的小型键值存储。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        if self.path.exists():
            self._state = json.loads(self.path.read_text(encoding="utf-8"))
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._state: dict[str, Any] = {}
            self._persist_locked()

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return json.loads(json.dumps(self._state.get(key, default)))

    def require(self, key: str, default: Any) -> Any:
        """取状态段；不存在时把 ``default`` 落盘后返回其副本。"""
        with self._lock:
            if key not in self._state:
                self._state[key] = default
                self._persist_locked()
            return json.loads(json.dumps(self._state[key]))

    def put(self, key: str, value: Any) -> None:
        with self._lock:
            self._state[key] = value
            self._persist_locked()

    def update(self, key: str, mutator) -> Any:
        """在锁内读取-修改-落盘一个状态段，避免读改写竞争。"""
        with self._lock:
            value = self._state.get(key)
            result = mutator(value)
            self._state[key] = result if result is not None else value
            self._persist_locked()
            return json.loads(json.dumps(self._state[key]))

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def _persist_locked(self) -> None:
        directory = self.path.parent
        fd, tmp_name = tempfile.mkstemp(prefix=".state-", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self._state, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self.path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
            raise
