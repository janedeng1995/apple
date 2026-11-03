# -*- coding: utf-8 -*-
"""抢购额度账本。

同时维护两层额度：
  · 任务额度：某个账号 + SKU 最多买几台；
  · 取货人额度：多个账号共用同一取货人时，合计最多买几台。

下单前先 reserve，成单（或已受理但未确认）后 commit，其余情况 release。
这样两个账号同时命中时，不会给同一取货人重复建单。
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid


class Ledger:
    SCHEMA_VERSION = 2

    def __init__(self, path: str):
        self.path = path
        self.lock = threading.RLock()
        self.data = self._empty()
        self._reservations = {}
        self.load()

    @classmethod
    def _empty(cls):
        return {"schema_version": cls.SCHEMA_VERSION, "by_task": {},
                "by_buyer": {}, "events": []}

    @staticmethod
    def _total(bucket: dict, key: str) -> int:
        return sum(int(v) for v in ((bucket or {}).get(key) or {}).values())

    def load(self):
        try:
            d = json.load(open(self.path, encoding="utf-8"))
            if isinstance(d, dict):
                self.data = d
        except Exception:
            pass

    def _save_unlocked(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def save(self):
        try:
            with self.lock:
                self._save_unlocked()
        except Exception:
            pass

    def migrate_legacy(self, tasks) -> None:
        """把旧版实际以“任务名”存在 by_buyer 下的数据迁移到 v2。"""
        if int(self.data.get("schema_version", 0) or 0) >= self.SCHEMA_VERSION:
            self.data.setdefault("by_task", {})
            self.data.setdefault("by_buyer", {})
            self.data.setdefault("events", [])
            return
        old = self.data.get("by_buyer") or {}
        fresh = self._empty()
        by_name = {getattr(t, "name", ""): t for t in tasks if getattr(t, "name", "")}
        for old_name, counts in old.items():
            task = by_name.get(old_name)
            if task is None or not isinstance(counts, dict):
                continue
            tid = getattr(task, "id", "")
            bid = getattr(task, "buyer_id", "")
            if tid:
                fresh["by_task"][tid] = {k: int(v) for k, v in counts.items()}
            if bid:
                dst = fresh["by_buyer"].setdefault(bid, {})
                for sku, n in counts.items():
                    dst[sku] = int(dst.get(sku, 0)) + int(n)
        fresh["events"] = list(self.data.get("events") or [])[-200:]
        fresh["events"].append({"t": time.strftime("%Y-%m-%d %H:%M:%S"),
                                "event": "ledger_migrated_v2"})
        self.data = fresh
        self.save()

    def task_total(self, task_id: str) -> int:
        with self.lock:
            return self._total(self.data.get("by_task") or {}, task_id)

    def buyer_total(self, buyer_id: str) -> int:
        with self.lock:
            return self._total(self.data.get("by_buyer") or {}, buyer_id)

    def task_got(self, task_id: str, sku: str) -> int:
        with self.lock:
            return int(((self.data.get("by_task") or {}).get(task_id) or {}).get(sku, 0))

    def buyer_got(self, buyer_id: str, sku: str) -> int:
        with self.lock:
            return int(((self.data.get("by_buyer") or {}).get(buyer_id) or {}).get(sku, 0))

    def _reserved_total(self, key: str, field: str) -> int:
        return sum(int(r["n"]) for r in self._reservations.values() if r.get(field) == key)

    def available(self, task_id: str, buyer_id: str,
                  task_limit: int, buyer_limit: int, include_reserved: bool = False) -> int:
        with self.lock:
            tr = self._reserved_total(task_id, "task_id") if include_reserved else 0
            br = self._reserved_total(buyer_id, "buyer_id") if include_reserved else 0
            task_left = max(0, int(task_limit) - self._total(self.data.get("by_task") or {}, task_id) - tr)
            buyer_left = max(0, int(buyer_limit) - self._total(self.data.get("by_buyer") or {}, buyer_id) - br)
            return min(task_left, buyer_left)

    def reserve(self, task_id: str, buyer_id: str, requested: int,
                task_limit: int, buyer_limit: int):
        """返回 (token, 可下单数量)；无额度时返回 ("", 0)。"""
        with self.lock:
            left = self.available(task_id, buyer_id, task_limit, buyer_limit,
                                  include_reserved=True)
            n = min(max(1, int(requested or 1)), left)
            if n <= 0:
                return "", 0
            token = uuid.uuid4().hex
            self._reservations[token] = {"task_id": task_id, "buyer_id": buyer_id, "n": n}
            return token, n

    def release(self, token: str) -> None:
        if not token:
            return
        with self.lock:
            self._reservations.pop(token, None)

    def commit(self, token: str, sku: str, task_name: str = "", buyer_name: str = "") -> int:
        with self.lock:
            r = self._reservations.pop(token, None)
            if not r:
                return 0
            n = int(r["n"])
            for bucket_name, key in (("by_task", r["task_id"]), ("by_buyer", r["buyer_id"])):
                row = self.data.setdefault(bucket_name, {}).setdefault(key, {})
                row[sku] = int(row.get(sku, 0)) + n
            self.data["schema_version"] = self.SCHEMA_VERSION
            self.data.setdefault("events", []).append({
                "t": time.strftime("%Y-%m-%d %H:%M:%S"), "event": "order",
                "task_id": r["task_id"], "buyer_id": r["buyer_id"],
                "task": task_name, "buyer": buyer_name, "sku": sku, "n": n,
            })
            self.data["events"] = self.data["events"][-200:]
            self._save_unlocked()
            return n

    def reset(self, task_id: str = "", buyer_id: str = "", sku: str = ""):
        with self.lock:
            if not task_id and not buyer_id:
                self.data = self._empty()
            else:
                for bucket_name, key in (("by_task", task_id), ("by_buyer", buyer_id)):
                    if not key:
                        continue
                    bucket = self.data.setdefault(bucket_name, {})
                    if sku:
                        bucket.get(key, {}).pop(sku, None)
                    else:
                        bucket.pop(key, None)
            self._reservations.clear()
            self._save_unlocked()

    def snapshot(self) -> dict:
        with self.lock:
            return {"by_task": dict(self.data.get("by_task") or {}),
                    "by_buyer": dict(self.data.get("by_buyer") or {})}
