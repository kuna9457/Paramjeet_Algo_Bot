"""Persistence: MongoDB (MONGO_URI) or local JSON files fallback.

Collections: signals, combos, orders, events, tokens, daily
"""
from __future__ import annotations

import json
import logging
import threading
import time
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


class ResilientMongoStorage(Storage):
    """MongoDB that never gives up.

    - Connects with a few retries at start. If MongoDB is still unreachable it keeps working on local JSON files
      and retries in the background every RETRY_SEC seconds (and on use) - no manual "Reconnect" needed.
    - Writes made while offline are queued in a file (data_cache/_mongo_pending_<role>.json) and replayed into
      MongoDB, in order, as soon as it is back. Nothing written while offline is lost.
    - Trading-state collections (MIRROR: open combos, broker tokens, settings) are also kept in the local files
      while online and refreshed from MongoDB on every (re)connect, so a MongoDB outage in the middle of a trade
      still lets the bot see and close its open combos.
    """
    RETRY_SEC = 30
    MIRROR = ("combos", "tokens", "config")

    def __init__(self, uri: str, db: str, local_folder: Path, role: str = "app", connect_tries: int = 2,
                 background: bool = True, mongo_factory=None):
        self.uri, self.dbname = uri, db or "nexus_smc_bot"
        self.local = LocalStorage(local_folder)
        self.pending_coll = f"_mongo_pending_{role}"
        self.lock = threading.RLock()
        self.mongo: Optional[MongoStorage] = None
        self.last_error = ""
        self.connected_at: Optional[str] = None
        self._last_try = 0.0
        self._factory = mongo_factory or MongoStorage
        for i in range(max(1, connect_tries)):
            if self.reconnect():
                break
            if i < connect_tries - 1:
                time.sleep(min(2 * (i + 1), 5))
        if background:
            threading.Thread(target=self._watch, name="mongo-reconnect", daemon=True).start()

    # ------------------------------------------------------------------ status
    @property
    def online(self) -> bool:
        return self.mongo is not None

    def pending_count(self) -> int:
        return len(self.local._load(self.pending_coll))

    def status(self) -> Dict[str, Any]:
        return {"online": self.online, "db": self.dbname, "last_error": self.last_error,
                "connected_at": self.connected_at, "pending_writes": self.pending_count()}

    # ------------------------------------------------------------------ connection
    def reconnect(self) -> bool:
        """Try to (re)connect now; replays queued offline writes and refreshes the local mirror. True if online."""
        self._last_try = time.time()
        try:
            m = self.mongo or self._factory(self.uri, self.dbname)      # slow part (up to 8 s) outside the lock
        except Exception as e:  # noqa
            with self.lock:
                self._offline(e)
            return False
        with self.lock:
            try:
                self._replay(m)
                self._refresh_mirror(m)
            except Exception as e:  # noqa - any failure: stay / go offline and retry later
                if m is not self.mongo:
                    try:
                        m.client.close()
                    except Exception:  # noqa
                        pass
                self._offline(e)
                return False
            if self.mongo is None:
                log.info("MongoDB online (%s)", self.dbname)
            self.mongo = m
            self.last_error = ""
            self.connected_at = datetime.now().isoformat(timespec="seconds")
            return True

    def _watch(self):
        while True:
            time.sleep(self.RETRY_SEC)
            if self.mongo is None:
                self.reconnect()

    def _offline(self, e: Exception):
        if self.mongo is not None or not self.last_error:
            log.error("MongoDB unavailable (%s) - using local files, retrying every %ss", e, self.RETRY_SEC)
        if self.mongo is not None:
            try:
                self.mongo.client.close()
            except Exception:  # noqa
                pass
        self.mongo = None
        self.last_error = f"{type(e).__name__}: {e}"[:300]

    def _replay(self, m: MongoStorage):
        ops = self.local._load(self.pending_coll)
        while ops:
            op = ops[0]
            if op["op"] == "insert":
                m.insert(op["coll"], op["doc"])
            else:
                m.upsert(op["coll"], op["query"], op["doc"])
            ops.pop(0)
            self.local._save(self.pending_coll, ops)      # progress kept if the connection drops mid-replay
        if ops == [] and self.local._path(self.pending_coll).exists():
            self.local._path(self.pending_coll).unlink()

    def _refresh_mirror(self, m: MongoStorage):
        for coll in self.MIRROR:
            with self.local.lock:
                self.local._save(coll, m.find(coll))

    # ------------------------------------------------------------------ operations
    def _read(self, name, *args):
        with self.lock:
            if self.mongo is not None:
                try:
                    return getattr(self.mongo, name)(*args)
                except Exception as e:  # noqa
                    self._offline(e)
            return getattr(self.local, name)(*args)

    def _write(self, name, coll, *args):
        with self.lock:
            if self.mongo is not None:
                try:
                    getattr(self.mongo, name)(coll, *args)
                    if coll in self.MIRROR:
                        getattr(self.local, name)(coll, *args)
                    return
                except Exception as e:  # noqa
                    self._offline(e)
            getattr(self.local, name)(coll, *args)
            op = {"op": name, "coll": coll}
            if name == "insert":
                op["doc"] = _clean(args[0])
            else:
                op["query"], op["doc"] = _clean(args[0]), _clean(args[1])
            with self.local.lock:
                ops = self.local._load(self.pending_coll)
                ops.append(op)
                self.local._save(self.pending_coll, ops)

    def find(self, coll, query=None):
        return self._read("find", coll, query)

    def find_one(self, coll, query):
        return self._read("find_one", coll, query)

    def insert(self, coll, doc):
        self._write("insert", coll, doc)

    def upsert(self, coll, query, doc):
        self._write("upsert", coll, query, doc)


def make_storage(backend: str, uri: str, db: str, local_folder: Path, role: str = "app") -> Storage:
    if backend == "mongo":
        if uri:
            return ResilientMongoStorage(uri, db or "nexus_smc_bot", local_folder, role=role)
        log.warning("MONGO_URI not set - using local JSON storage")
    return LocalStorage(local_folder)
