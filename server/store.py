"""SQLite persistence: an append-only event log plus tables for replay and export."""
from __future__ import annotations

import json
import os
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS agents (id INTEGER PRIMARY KEY, name TEXT UNIQUE, key TEXT, is_house INTEGER,
    created_at REAL, removed INTEGER DEFAULT 0, card TEXT, coins INTEGER, items TEXT);
CREATE TABLE IF NOT EXISTS rounds (id INTEGER PRIMARY KEY, mode TEXT, started_at REAL, ends_at REAL,
    ended_at REAL, setup TEXT, results TEXT);
CREATE TABLE IF NOT EXISTS chats (id INTEGER PRIMARY KEY, round_id INTEGER, agent_a INTEGER, agent_b INTEGER,
    status TEXT, whose_turn INTEGER, last_message_at REAL, end_reason TEXT, opened_at REAL, ended_at REAL);
CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, round_id INTEGER,
    author INTEGER, text TEXT, action TEXT, offer TEXT, at REAL, system INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS deals (id INTEGER PRIMARY KEY, round_id INTEGER, chat_id INTEGER, seller INTEGER,
    buyer INTEGER, terms TEXT, at REAL);
CREATE TABLE IF NOT EXISTS posts (id INTEGER PRIMARY KEY, round_id INTEGER, author TEXT, author_id INTEGER,
    text TEXT, at REAL);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL, kind TEXT, agent INTEGER,
    data TEXT);
CREATE INDEX IF NOT EXISTS ix_msg_round ON messages(round_id);
CREATE INDEX IF NOT EXISTS ix_deal_round ON deals(round_id);
CREATE INDEX IF NOT EXISTS ix_post_round ON posts(round_id);
"""


def _j(x):
    return json.dumps(x, separators=(",", ":"))


class Store:
    def __init__(self, path: str = ":memory:"):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        if path != ":memory:":
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)

    # ---- meta ----
    def get_meta(self, k, default=None):
        r = self.db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return r["v"] if r else default

    def set_meta(self, k, v):
        self.db.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", (k, str(v)))

    # ---- event log ----
    def log(self, kind: str, agent: int | None = None, **data):
        self.db.execute("INSERT INTO events(at,kind,agent,data) VALUES(?,?,?,?)",
                        (time.time(), kind, agent, _j(data)))

    # ---- agents ----
    def save_agent(self, a):
        self.db.execute(
            "INSERT INTO agents(id,name,key,is_house,created_at,card,coins,items) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, card=excluded.card, coins=excluded.coins, "
            "items=excluded.items, removed=0",
            (a.id, a.name, a.key, int(a.is_house), a.created_at, _j(a.card), a.coins, _j(a.items())))

    def remove_agent(self, agent_id):
        self.db.execute("UPDATE agents SET removed=1 WHERE id=?", (agent_id,))

    def load_agents(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM agents WHERE removed=0 ORDER BY id")]

    def removed_names(self):
        return {r["name"].lower() for r in self.db.execute("SELECT name FROM agents WHERE removed=1")}

    # ---- rounds ----
    def save_round(self, rnd: dict, setup: dict):
        self.db.execute("INSERT OR REPLACE INTO rounds(id,mode,started_at,ends_at,setup) VALUES(?,?,?,?,?)",
                        (rnd["id"], rnd["mode"], rnd["started_at"], rnd.get("ends_at"), _j(setup)))

    def update_round_end(self, round_id: int, ends_at: float | None):
        self.db.execute("UPDATE rounds SET ends_at=? WHERE id=?", (ends_at, round_id))

    def end_round(self, round_id: int, ended_at: float, results: dict | None):
        self.db.execute("UPDATE rounds SET ended_at=?, results=? WHERE id=?", (ended_at, _j(results), round_id))

    def last_round(self):
        r = self.db.execute("SELECT * FROM rounds ORDER BY id DESC LIMIT 1").fetchone()
        return dict(r) if r else None

    def get_round(self, rid):
        r = self.db.execute("SELECT * FROM rounds WHERE id=?", (rid,)).fetchone()
        return dict(r) if r else None

    def rounds(self, mode=None, limit=50):
        q = "SELECT id,mode,started_at,ends_at,ended_at,results FROM rounds"
        args = ()
        if mode:
            q += " WHERE mode=?"
            args = (mode,)
        q += " ORDER BY id DESC LIMIT ?"
        return [dict(r) for r in self.db.execute(q, args + (limit,))]

    def max_id(self, table):
        r = self.db.execute(f"SELECT MAX(id) AS m FROM {table}").fetchone()
        return r["m"] or 0

    # ---- chats / messages / deals / posts ----
    def save_chat(self, c):
        self.db.execute(
            "INSERT OR REPLACE INTO chats(id,round_id,agent_a,agent_b,status,whose_turn,last_message_at,end_reason,"
            "opened_at,ended_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (c.id, c.round_id, c.a, c.b, c.status, c.whose_turn, c.last_message_at, c.end_reason, c.opened_at,
             c.ended_at))

    def save_message(self, chat, m: dict):
        self.db.execute("INSERT INTO messages(chat_id,round_id,author,text,action,offer,at,system) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (chat.id, chat.round_id, m["author"], m["text"], m["action"], _j(m.get("offer")), m["at"],
                         int(m.get("system", False))))

    def save_deal(self, d: dict):
        self.db.execute("INSERT INTO deals(id,round_id,chat_id,seller,buyer,terms,at) VALUES(?,?,?,?,?,?,?)",
                        (d["id"], d["round_id"], d["chat_id"], d["a_id"], d["b_id"], _j(d), d["at"]))

    def save_post(self, p: dict):
        self.db.execute("INSERT INTO posts(id,round_id,author,author_id,text,at) VALUES(?,?,?,?,?,?)",
                        (p["id"], p["round_id"], p["author"], p.get("author_id"), p["text"], p["at"]))

    def round_deals(self, rid):
        return [json.loads(r["terms"]) for r in self.db.execute("SELECT terms FROM deals WHERE round_id=? ORDER BY id",
                                                                (rid,))]

    def round_posts(self, rid):
        return [dict(r) for r in self.db.execute("SELECT * FROM posts WHERE round_id=? ORDER BY id", (rid,))]

    def round_chats(self, rid):
        return [dict(r) for r in self.db.execute("SELECT * FROM chats WHERE round_id=? ORDER BY id", (rid,))]

    def round_messages(self, rid):
        out = []
        for r in self.db.execute("SELECT * FROM messages WHERE round_id=? ORDER BY id", (rid,)):
            d = dict(r)
            d["offer"] = json.loads(d["offer"]) if d["offer"] else None
            out.append(d)
        return out
