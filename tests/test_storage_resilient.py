"""MongoDB storage that reconnects by itself and never loses writes made while it was down."""
from bot.services.storage import LocalStorage, ResilientMongoStorage, make_storage


class Down(Exception):
    pass


class FakeMongo:
    """Stands in for MongoStorage; `net["up"]` simulates the network / Atlas being reachable."""
    net = {"up": True}
    data = {}

    def __init__(self, uri, db):
        if not self.net["up"]:
            raise Down("server selection timeout")
        self.client = self

    def close(self):
        pass

    def _chk(self):
        if not self.net["up"]:
            raise Down("connection lost")

    def find(self, coll, query=None):
        self._chk()
        return [d for d in self.data.get(coll, []) if all(d.get(k) == v for k, v in (query or {}).items())]

    def find_one(self, coll, query):
        r = self.find(coll, query)
        return r[0] if r else None

    def insert(self, coll, doc):
        self._chk()
        self.data.setdefault(coll, []).append(dict(doc))

    def upsert(self, coll, query, doc):
        self._chk()
        for d in self.data.setdefault(coll, []):
            if all(d.get(k) == v for k, v in query.items()):
                d.update(doc)
                return
        self.data[coll].append({**query, **doc})


def make(tmp_path, up=True):
    FakeMongo.net["up"], FakeMongo.data = up, {}
    return ResilientMongoStorage("mongodb://x", "db", tmp_path, role="test", connect_tries=1, background=False,
                                 mongo_factory=FakeMongo)


def test_starts_offline_then_connects_and_replays_writes(tmp_path):
    st = make(tmp_path, up=False)
    assert not st.online and "timeout" in st.status()["last_error"]
    st.insert("events", {"kind": "ENTRY", "n": 1})                  # written while MongoDB is down
    st.upsert("combos", {"id": "c1"}, {"status": "OPEN"})
    assert st.find("events")[0]["n"] == 1 and st.pending_count() == 2
    FakeMongo.net["up"] = True
    assert st.reconnect() and st.online
    assert FakeMongo.data["events"] == [{"kind": "ENTRY", "n": 1}]   # replayed in order
    assert FakeMongo.data["combos"] == [{"id": "c1", "status": "OPEN"}]
    assert st.pending_count() == 0
    st.insert("events", {"n": 2})
    assert len(FakeMongo.data["events"]) == 2


def test_connection_lost_mid_session_keeps_working_and_sees_open_combo(tmp_path):
    st = make(tmp_path)
    st.upsert("combos", {"id": "c1"}, {"status": "OPEN"})            # mirrored locally while online
    FakeMongo.net["up"] = False
    assert st.find("combos")[0]["status"] == "OPEN"                  # outage: still sees the open trade
    assert not st.online
    st.upsert("combos", {"id": "c1"}, {"status": "CLOSED"})
    FakeMongo.net["up"] = True
    assert st.reconnect()
    assert FakeMongo.data["combos"][0]["status"] == "CLOSED"


def test_mirror_is_refreshed_from_mongo_on_connect(tmp_path):
    LocalStorage(tmp_path).upsert("combos", {"id": "stale"}, {"status": "OPEN"})   # old local leftovers
    FakeMongo.net["up"], FakeMongo.data = True, {"combos": [{"id": "real", "status": "OPEN"}]}
    st = ResilientMongoStorage("mongodb://x", "db", tmp_path, role="t", connect_tries=1, background=False,
                               mongo_factory=FakeMongo)
    FakeMongo.net["up"] = False
    assert [c["id"] for c in st.find("combos")] == ["real"]


def test_failed_replay_keeps_remaining_writes(tmp_path):
    st = make(tmp_path, up=False)
    for i in range(3):
        st.insert("events", {"n": i})
    FakeMongo.net["up"] = True
    calls = {"n": 0}
    orig = FakeMongo.insert

    def flaky(self, coll, doc):
        calls["n"] += 1
        if calls["n"] == 2:
            raise Down("dropped")
        orig(self, coll, doc)
    FakeMongo.insert = flaky
    try:
        assert not st.reconnect()
    finally:
        FakeMongo.insert = orig
    assert st.pending_count() == 2 and [d["n"] for d in FakeMongo.data["events"]] == [0]
    assert st.reconnect() and [d["n"] for d in FakeMongo.data["events"]] == [0, 1, 2]


def test_no_uri_means_plain_local(tmp_path):
    assert isinstance(make_storage("mongo", "", "db", tmp_path), LocalStorage)
    assert isinstance(make_storage("local", "mongodb://x", "db", tmp_path), LocalStorage)
