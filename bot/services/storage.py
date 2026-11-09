"""Persistence: MongoDB (MONGO_URI) or local JSON files fallback.

Collections: signals, combos, orders, events, tokens, daily
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger("storage")


def _clean(o: Any):
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items() if k != "_id"}
    if isinstance(o, list):
        return [_clean(v) for v in o]
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    return o


class Storage:
    def find(self, coll: str, query: Dict | None = None) -> List[Dict]: ...
    def find_one(self, coll: str, query: Dict) -> Optional[Dict]: ...
    def insert(self, coll: str, doc: Dict) -> None: ...
    def upsert(self, coll: str, query: Dict, doc: Dict) -> None: ...


class MongoStorage(Storage):
    def __init__(self, uri: str, db: str):
        from pymongo import MongoClient
        self.client = MongoClient(uri, serverSelectionTimeoutMS=8000, tz_aware=True)
        self.client.admin.command("ping")
        self.db = self.client[db]
        self.db["combos"].create_index("id", unique=True)
        log.info("MongoDB connected (%s)", db)

    def find(self, coll, query=None):
        return [_clean(d) for d in self.db[coll].find(query or {})]

    def find_one(self, coll, query):
        d = self.db[coll].find_one(query)
        return _clean(d) if d else None

    def insert(self, coll, doc):
        self.db[coll].insert_one(_clean(doc))

    def upsert(self, coll, query, doc):
        self.db[coll].update_one(query, {"$set": _clean(doc)}, upsert=True)


class LocalStorage(Storage):
    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()

    def _path(self, coll):
        return self.folder / f"{coll}.json"

    def _load(self, coll) -> List[Dict]:
        p = self._path(coll)
        if not p.exists():
            return []
        return json.loads(p.read_text(encoding="utf-8") or "[]")

    def _save(self, coll, rows):
        self._path(coll).write_text(json.dumps(rows, indent=1, default=str), encoding="utf-8")

    @staticmethod
    def _match(d, q):
        return all(d.get(k) == v for k, v in (q or {}).items())

    def find(self, coll, query=None):
        with self.lock:
            return [d for d in self._load(coll) if self._match(d, query)]

    def find_one(self, coll, query):
        r = self.find(coll, query)
        return r[0] if r else None

    def insert(self, coll, doc):
        with self.lock:
            rows = self._load(coll)
            rows.append(_clean(doc))
            self._save(coll, rows)

    def upsert(self, coll, query, doc):
        with self.lock:
            rows = self._load(coll)
            for r in rows:
                if self._match(r, query):
                    r.update(_clean(doc))
                    break
            else:
                new = dict(query)
                new.update(_clean(doc))
                rows.append(new)
            self._save(coll, rows)


def make_storage(backend: str, uri: str, db: str, local_folder: Path) -> Storage:
    if backend == "mongo":
        if not uri:
            log.warning("MONGO_URI not set - falling back to local JSON storage")
        else:
            try:
                return MongoStorage(uri, db or "nexus_smc_bot")
            except Exception as e:  # noqa
                log.error("MongoDB connection failed (%s) - falling back to local JSON storage", e)
    return LocalStorage(local_folder)
