"""The Espresso Exchange market engine.

Synchronous and single-threaded by design: the web layer calls these methods from one
asyncio event loop, so every act() runs to completion before the next one starts. That is
the "one queue" from the spec: two deals can never spend the same coin.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import random
import re
import secrets
import time
import unicodedata
from collections import deque
from dataclasses import dataclass, field

from .config import Config
from .items import FULL_NAMES, ITEMS, SERIAL_PREFIX, fmt_goods, fmt_offer, normalize_offer, value

HOUSE_NAMES = ["Fair Trader", "Hardliner", "Wildcard", "Opener"]
LEGACY_HOUSE_NAMES = {"Liar": "Wildcard"}  # renamed house bots, migrated on restore
HOUSE_STYLES = {
    "Fair Trader": "Tells the truth, accepts any offer that gains it 10 or more, counters halfway",
    "Hardliner": "Opens at double its value, concedes slowly, threatens to walk",
    "Wildcard": "Talks big: hypes its own goods and spreads market rumors on the board",
    "Opener": "Starts chats with every new bot, so yours gets approached too",
}
RESERVED = {"server", "house", "host", "admin", "system", "you", "me", "moderator", "referee", "exchange"}
ACTION_ALIASES = {
    "talk": "talk", "say": "talk", "chat": "talk", "message": "talk", "ask": "talk", "reply": "talk",
    "respond": "talk", "question": "talk", "bluff": "talk", "threaten": "talk", "inform": "talk",
    "offer": "offer", "counter": "offer", "counteroffer": "offer", "counter_offer": "offer",
    "counter-offer": "offer", "propose": "offer", "proposal": "offer", "bid": "offer", "ask_price": "offer",
    "accept": "accept", "deal": "accept", "agree": "accept", "yes": "accept", "take": "accept",
    "end": "end", "walk": "end", "walk_away": "end", "walkaway": "end", "leave": "end", "quit": "end",
    "reject": "end", "no_deal": "end", "close": "end", "exit": "end", "stop": "end",
}
CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class GameError(Exception):
    """A request the game refuses (shown to the caller)."""


def _clean_text(x) -> str:
    if x is None:
        return ""
    if isinstance(x, dict):  # LLMs sometimes wrap text: {"text": "..."} or {"message": "..."}
        inner = next((x[k] for k in ("text", "message", "content", "post") if isinstance(x.get(k), str)), None)
        x = inner if inner is not None else json.dumps(x)
    if not isinstance(x, str):
        x = json.dumps(x) if isinstance(x, list) else str(x)
    return CONTROL_CHARS.sub("", x).strip()


def _mmss(seconds: float) -> str:
    s = max(0, int(round(seconds)))
    return f"{s // 60:02d}:{s % 60:02d}"


@dataclass
class Agent:
    id: int
    name: str
    key: str
    is_house: bool = False
    created_at: float = 0.0
    active: bool = False
    coins: int = 0
    units: dict = field(default_factory=lambda: {t: [] for t in ITEMS})
    card: dict = field(default_factory=dict)
    start_wealth: int = 0
    start_coins: int = 0
    post_times: deque = field(default_factory=deque)
    market_due_at: float | None = None
    market_seq: int = 0
    market_ids: list = field(default_factory=list)
    notices: list = field(default_factory=list)
    recent_notices: deque = field(default_factory=lambda: deque(maxlen=40))
    open_chats: set = field(default_factory=set)
    game_over_delivered: bool = False
    series: list = field(default_factory=list)
    last_seen: float = 0.0
    requests: int = 0

    def items(self) -> dict:
        return {t: len(self.units[t]) for t in ITEMS}

    def wealth(self) -> int:
        return self.coins + sum(self.card.get(t, 0) * len(self.units[t]) for t in ITEMS)

    def holdings(self) -> dict:
        return {"coins": self.coins, "items": self.items()}


@dataclass
class Chat:
    id: int
    round_id: int
    a: int
    b: int
    opened_at: float
    status: str = "open"            # open | deal | ended
    whose_turn: int = 0
    messages: list = field(default_factory=list)
    offers: dict = field(default_factory=dict)
    last_message_at: float = 0.0
    end_reason: str | None = None
    ended_at: float | None = None
    ended_by: int | None = None
    delivered_at: float | None = None

    @property
    def n_msgs(self) -> int:
        return sum(1 for m in self.messages if not m.get("system"))

    def other(self, aid: int) -> int:
        return self.b if aid == self.a else self.a


class Result:
    def __init__(self):
        self.applied: list[str] = []
        self.dropped: list[str] = []
        self.deal: dict | None = None

    def to_dict(self) -> dict:
        d = {"ok": not self.dropped, "applied": self.applied, "dropped": self.dropped}
        if self.deal:
            d["deal"] = self.deal
        return d


class Market:
    def __init__(self, config: Config | None = None, store=None, clock=time.time, rng: random.Random | None = None,
                 secret: str = "dev-secret"):
        self.cfg = config or Config()
        self.store = store
        self.clock = clock
        self.rng = rng or random.Random()
        self.secret = secret
        self.agents: dict[int, Agent] = {}
        self.by_key: dict[str, int] = {}
        self.by_name: dict[str, int] = {}
        self.removed_names: set[str] = set()
        self.next_agent_id = 1
        self.chats: dict[int, Chat] = {}
        self.open_chat_ids: set[int] = set()
        self.pair_chat: dict[tuple, int] = {}
        self.next_chat_id = 1
        self.next_deal_id = 1
        self.next_post_id = 1
        self.deals: list[dict] = []
        self.posts: list[dict] = []
        self.units: dict[str, dict] = {}
        self.round: dict | None = None
        self.round_seq = 0
        self.mode = "practice"          # practice | game | over | reveal
        self.registration_open = True
        self.practice_history: deque = deque(maxlen=30)
        self.archive: deque = deque(maxlen=3)
        self.final: dict | None = None
        self.house_notice: dict | None = None
        self.recent_ended: deque = deque(maxlen=12)
        self.next_market_tick: float | None = None
        self.on_ready = lambda agent_id: None
        self.key_epoch = 0
        if store is not None:
            self.next_agent_id = store.max_id("agents") + 1
            self.next_chat_id = store.max_id("chats") + 1
            self.next_deal_id = store.max_id("deals") + 1
            self.next_post_id = store.max_id("posts") + 1
            self.round_seq = store.max_id("rounds")
            self.removed_names = store.removed_names()
            self.key_epoch = int(store.get_meta("key_epoch", 0))

    # ------------------------------------------------------------------ helpers
    def _log(self, kind, agent=None, **data):
        if self.store is not None:
            self.store.log(kind, agent, **data)

    def _notify(self, aid: int):
        try:
            self.on_ready(aid)
        except Exception:  # pragma: no cover - never let a signal break the game
            pass

    def _notice(self, a: Agent, text: str):
        a.notices.append(text)
        a.recent_notices.append({"at": self.clock(), "text": text})
        if len(a.notices) > 30:
            del a.notices[:-30]

    def active_agents(self) -> list[Agent]:
        return [a for a in self.agents.values() if a.active]

    def teams(self) -> list[Agent]:
        return [a for a in self.agents.values() if not a.is_house]

    def market_open(self) -> bool:
        return self.round is not None and self.mode in ("practice", "game")

    # ------------------------------------------------------------------ keys and registration
    def make_key(self, name: str) -> str:
        label = name if not self.key_epoch else f"{self.key_epoch}:{name}"
        tag = hmac.new(self.secret.encode(), label.encode(), hashlib.sha256).hexdigest()[:24]
        nm = base64.urlsafe_b64encode(name.encode()).decode().rstrip("=")
        return f"esx_{nm}.{tag}"

    def name_from_key(self, key: str) -> str | None:
        if not key or not key.startswith("esx_") or "." not in key:
            return None
        nm, _tag = key[4:].rsplit(".", 1)
        try:
            name = base64.urlsafe_b64decode(nm + "=" * (-len(nm) % 4)).decode()
        except Exception:
            return None
        return name if hmac.compare_digest(self.make_key(name), key) else None

    @staticmethod
    def clean_name(raw) -> str:
        s = unicodedata.normalize("NFKC", str(raw or ""))   # fullwidth and styled letters become plain ones
        s = re.sub(r"\s+", " ", s).strip()
        s = re.sub(r"[^A-Za-z0-9_ \-\.'&!]", "", s).strip()  # ASCII only: no look-alike letters
        if not 2 <= len(s) <= 24:
            raise GameError("Team name must be 2 to 24 characters: letters, digits, spaces and - _ . ' & !")
        if s.lower() in RESERVED or s.lower() in {h.lower() for h in HOUSE_NAMES}:
            raise GameError(f"The name '{s}' is reserved. Pick another.")
        return s

    def register(self, raw_name, is_house: bool = False) -> Agent:
        name = raw_name if is_house else self.clean_name(raw_name)
        if not is_house:
            if not self.registration_open:
                raise GameError("Registration is closed while the game runs.")
            if name.lower() in self.by_name:
                raise GameError(f"The name '{name}' is taken. If it is yours, ask the host for your key.")
            if name.lower() in self.removed_names:
                raise GameError(f"The name '{name}' was removed by the host.")
            if len(self.teams()) >= self.cfg.max_teams:
                raise GameError("The market is full.")
        key = self.make_key(name) if not is_house else "house_" + secrets.token_urlsafe(18)
        a = Agent(id=self.next_agent_id, name=name, key=key, is_house=is_house, created_at=self.clock())
        self.next_agent_id += 1
        self.agents[a.id] = a
        self.by_key[key] = a.id
        self.by_name[name.lower()] = a.id
        if self.store is not None:
            self.store.save_agent(a)
        self._log("register", a.id, name=name, house=is_house)
        if self.round is not None and self.mode == "practice":
            self._join_round(a)
        return a

    def ensure_house_bots(self):
        for n in HOUSE_NAMES:
            if n.lower() not in self.by_name:
                self.register(n, is_house=True)

    def agent_for_key(self, key: str | None) -> Agent | None:
        if not key:
            return None
        aid = self.by_key.get(key)
        if aid is not None:
            return self.agents.get(aid)
        # Signed key from before a restart that lost the disk: re-create the team.
        name = self.name_from_key(key)
        if name and name.lower() not in self.by_name and name.lower() not in self.removed_names \
                and self.registration_open:
            try:
                return self.register(name)
            except GameError:
                return None
        return None

    def resolve_name(self, raw) -> Agent | None:
        if raw is None:
            return None
        s = re.sub(r"\s+", " ", str(raw)).strip().lstrip("@").lower()
        if not s:
            return None
        aid = self.by_name.get(s)
        if aid is not None and self.agents[aid].active:
            return self.agents[aid]
        cands = [a for a in self.active_agents() if a.name.lower().startswith(s) or s in a.name.lower()]
        return cands[0] if len(cands) == 1 else None

    def wipe_teams(self) -> int:
        """Fresh start before an event: every team is gone and every old key stops working."""
        now = self.clock()
        n = 0
        for a in list(self.teams()):
            for cid in list(a.open_chats):
                self._end_chat(self.chats[cid], "the host wiped all teams", now)
            self.by_key.pop(a.key, None)
            self.by_name.pop(a.name.lower(), None)
            del self.agents[a.id]
            n += 1
        self.removed_names.clear()
        self.practice_history.clear()
        self.archive.clear()
        self.key_epoch += 1
        if self.store is not None:
            self.store.db.execute("DELETE FROM agents WHERE is_house=0")
            self.store.set_meta("key_epoch", self.key_epoch)
        self._log("wipe", None, teams=n)
        self.registration_open = True
        self.start_round("practice")
        return n

    def remove_team(self, name: str) -> bool:
        a = self.resolve_name(name) or self.agents.get(self.by_name.get(str(name).lower(), -1))
        if a is None or a.is_house:
            return False
        now = self.clock()
        for cid in list(a.open_chats):
            self._end_chat(self.chats[cid], "the host removed a team", now)
        a.active = False
        self.by_key.pop(a.key, None)
        self.by_name.pop(a.name.lower(), None)
        self.removed_names.add(a.name.lower())
        del self.agents[a.id]
        if self.store is not None:
            self.store.remove_agent(a.id)
        self._log("remove", a.id, name=a.name)
        return True

    # ------------------------------------------------------------------ dealing
    def _latin_card(self, k: int) -> dict:
        perm = self.round["perm"]
        cols = self.round["cols"]
        return {cols[j]: perm[(j + k) % 5] for j in range(5)}

    def _new_unit(self, t: str, owner: int, now: float) -> str:
        n = self.round["next_serial"][t]
        self.round["next_serial"][t] = n + 1
        serial = f"{SERIAL_PREFIX[t]}-{n:02d}"
        self.units[serial] = {"type": t, "owner": owner, "history": [[owner, 0.0, None]]}
        return serial

    def _deal(self, agents: list[Agent], now: float):
        order = agents[:]
        self.rng.shuffle(order)
        for k, a in enumerate(order):
            a.card = self._latin_card(k)
        self.round["latin_k"] = len(order)
        pool = []
        for t in ITEMS:
            for _ in agents:
                pool.append(t)
        self.rng.shuffle(pool)
        for k, a in enumerate(agents):
            a.units = {t: [] for t in ITEMS}
            for t in pool[k * 5:(k + 1) * 5]:
                a.units[t].append(self._new_unit(t, a.id, now))
            for t in ITEMS:
                a.units[t].sort()
            a.coins = self.cfg.starting_wealth - value(a.card, a.items())
            a.start_wealth = a.wealth()
            a.start_coins = a.coins

    def _reset_agent_round_state(self, a: Agent, now: float):
        a.post_times = deque()
        a.market_ids = []
        a.open_chats = set()
        a.game_over_delivered = False
        a.series = []
        a.market_due_at = None

    def _join_round(self, a: Agent):
        """A team registering mid-practice gets a fresh hand at once."""
        now = self.clock()
        self._reset_agent_round_state(a, now)
        a.active = True
        a.card = self._latin_card(self.round["latin_k"])
        self.round["latin_k"] += 1
        a.units = {t: [] for t in ITEMS}
        for _ in range(5):
            t = self.rng.choice(ITEMS)
            a.units[t].append(self._new_unit(t, a.id, now))
        for t in ITEMS:
            a.units[t].sort()
        a.coins = self.cfg.starting_wealth - value(a.card, a.items())
        a.start_wealth = a.wealth()
        a.start_coins = a.coins
        a.series = [[round(now - self.round["started_at"], 1), a.wealth()]]
        a.market_due_at = now
        self._log("join_round", a.id, round=self.round["id"], coins=a.coins, items=a.items(), card=a.card)
        self._notify(a.id)

    def _setup_snapshot(self) -> dict:
        return {"agents": {str(a.id): {"name": a.name, "coins": a.coins, "card": a.card,
                                       "units": {t: list(a.units[t]) for t in ITEMS}, "start_wealth": a.start_wealth}
                           for a in self.active_agents()},
                "perm": self.round["perm"], "cols": self.round["cols"],
                "next_serial": self.round["next_serial"]}

    def start_round(self, mode: str):
        assert mode in ("practice", "game")
        now = self.clock()
        self._close_round(now, "market reset" if mode == "practice" else "the host started the game")
        self.round_seq += 1
        self.mode = mode
        self.final = None
        perm = list((10, 30, 60, 100, 200))
        self.rng.shuffle(perm)
        cols = ITEMS[:]
        self.rng.shuffle(cols)
        self.round = {"id": self.round_seq, "mode": mode, "started_at": now, "perm": perm, "cols": cols,
                      "next_serial": {t: 1 for t in ITEMS}, "latin_k": 0, "last_call_done": False,
                      "ends_at": now + self.cfg.game_minutes * 60 if mode == "game" else None,
                      "reset_at": now + self.cfg.practice_reset_minutes * 60 if mode == "practice" else None}
        self.units = {}
        self.deals = []
        self.posts = []
        self.recent_ended = deque(maxlen=12)
        active = [a for a in self.agents.values() if (not a.is_house) or mode == "practice" or self.cfg.house_in_game]
        for a in self.agents.values():
            self._reset_agent_round_state(a, now)
            a.active = a in active
            if not a.active:
                a.units = {t: [] for t in ITEMS}
                a.coins = 0
                a.card = {}
        self._deal(active, now)
        for a in active:
            a.series = [[0.0, a.wealth()]]
            a.market_due_at = now
            if mode == "game":
                self._notice(a, "The game has started: new hands and cards are dealt. 25 minutes to the bell."
                             if self.cfg.game_minutes == 25 else
                             f"The game has started: new hands and cards are dealt. "
                             f"{self.cfg.game_minutes:g} minutes to the bell.")
            else:
                self._notice(a, f"Practice round {self.round['id']} opened with fresh hands and cards.")
        self.next_market_tick = now + self.cfg.market_interval_s
        if self.store is not None:
            self.store.save_round(self.round, self._setup_snapshot())
        self._log("round_start", None, round=self.round["id"], mode=mode, agents=[a.id for a in active])
        for a in active:
            self._notify(a.id)

    def _close_round(self, now: float, reason: str):
        if self.round is None:
            return
        for cid in list(self.open_chat_ids):
            self._end_chat(self.chats[cid], reason, now)
        results = None
        if self.round["mode"] == "practice":
            gains = {a.name: a.wealth() - a.start_wealth for a in self.active_agents()}
            wealth = {a.name: a.wealth() for a in self.active_agents()}
            entry = {"round": self.round["id"], "started_at": self.round["started_at"], "ended_at": now,
                     "gains": gains, "wealth": wealth, "deals": len(self.deals)}
            self.practice_history.append(entry)
            results = entry
        elif self.final:
            results = self.final
        self.archive.append({"round": self.round["id"], "mode": self.round["mode"],
                             "chats": [c for c in self.chats.values() if c.round_id == self.round["id"]],
                             "deals": self.deals, "posts": self.posts})
        # keep only chats of rounds still in the archive
        keep = {r["round"] for r in self.archive}
        for cid in [cid for cid, c in self.chats.items() if c.round_id not in keep]:
            del self.chats[cid]
        if self.store is not None:
            self.store.end_round(self.round["id"], now, results)
        self._log("round_end", None, round=self.round["id"], reason=reason)

    # ------------------------------------------------------------------ mode changes (host)
    def start_game(self):
        self.registration_open = False
        self.start_round("game")

    def to_practice(self):
        self.registration_open = True
        self.start_round("practice")

    def ring_bell(self):
        if self.mode == "game":
            self._bell(self.clock())

    def reveal(self) -> bool:
        if self.mode in ("over", "reveal") and self.final:
            self.mode = "reveal"
            self._log("reveal", None)
            return True
        return False

    def post_house_notice(self, text: str):
        text = _clean_text(text)[:300]
        if not text:
            self.house_notice = None
            return
        now = self.clock()
        self.house_notice = {"text": text, "at": now}
        self._add_post(None, "HOUSE", text, now)
        for a in self.active_agents():
            self._notice(a, f"HOUSE NOTICE (from the host, via the server): {text}")
            self._notify(a.id)

    # ------------------------------------------------------------------ clock
    def clock_info(self, now: float | None = None) -> dict:
        now = self.clock() if now is None else now
        if self.round is None:
            return {"mode": self.mode}
        if self.round["mode"] == "game":
            total = self.round["ends_at"] - self.round["started_at"]
            elapsed = min(now, self.round["ends_at"]) - self.round["started_at"]
            left = max(0.0, self.round["ends_at"] - now) if self.mode == "game" else 0.0
            return {"mode": self.mode, "round": self.round["id"],
                    "game_clock": f"{_mmss(elapsed)} of {_mmss(total)}", "seconds_left": int(left),
                    "last_call": self.round["last_call_done"]}
        left = max(0.0, self.round["reset_at"] - now)
        return {"mode": "practice", "round": self.round["id"],
                "game_clock": f"practice round {self.round['id']}, resets in {_mmss(left)}",
                "seconds_left": int(left), "last_call": False}

    def tick(self, now: float | None = None):
        now = self.clock() if now is None else now
        if self.round is None:
            return
        for cid in list(self.open_chat_ids):
            c = self.chats[cid]
            if now - c.last_message_at >= self.cfg.reply_timeout_s:
                late = self.agents.get(c.whose_turn)
                self._end_chat(c, f"timeout: {late.name if late else '?'} did not reply within "
                                  f"{int(self.cfg.reply_timeout_s)} s", now)
        if self.market_open() and self.next_market_tick is not None and now >= self.next_market_tick:
            for a in self.active_agents():
                if a.market_due_at is None:
                    a.market_due_at = now
                    self._notify(a.id)
            self.next_market_tick = max(self.next_market_tick + self.cfg.market_interval_s,
                                        now + self.cfg.market_interval_s * 0.5)
        if self.mode == "practice" and now >= self.round["reset_at"]:
            self.start_round("practice")
        elif self.mode == "game":
            lc_at = self.round["started_at"] + self.cfg.last_call_minutes * 60
            if not self.round["last_call_done"] and now >= lc_at and lc_at < self.round["ends_at"]:
                self.round["last_call_done"] = True
                mins = max(1, round((self.round["ends_at"] - now) / 60))
                self._add_post(None, "HOUSE", f"Last call! {mins} minute{'s' if mins != 1 else ''} to the bell.", now)
                for a in self.active_agents():
                    self._notice(a, f"Last call: the bell rings in about {mins} minute(s).")
            if now >= self.round["ends_at"]:
                self._bell(now)

    # ------------------------------------------------------------------ events
    def _board_slice(self, now: float, n: int) -> list[dict]:
        return [{"id": p["id"], "author": p["author"], "text": p["text"], "ago_s": int(now - p["at"])}
                for p in self.posts[-n:]] if n else []

    def _ledger_slice(self, now: float, n: int) -> list[dict]:
        return [{"text": d["text"], "ago_s": int(now - d["at"])} for d in self.deals[-n:]] if n else []

    def _you(self, a: Agent) -> dict:
        return {"you": a.name, "holdings": a.holdings(), "card": dict(a.card), "wealth": a.wealth()}

    def _posts_left(self, a: Agent, now: float) -> int:
        while a.post_times and now - a.post_times[0] >= 60:
            a.post_times.popleft()
        return max(0, self.cfg.posts_per_minute - len(a.post_times))

    def _take_notices(self, a: Agent) -> list[str]:
        out, a.notices = a.notices, []
        return out

    def _turn_event(self, a: Agent, c: Chat, now: float) -> dict:
        partner = self.agents[c.other(a.id)]
        transcript = []
        for m in c.messages:
            transcript.append({"from": "SERVER" if m.get("system") else self.agents_name(m["author"]),
                               "action": m["action"], "text": m["text"], "offer": m.get("offer")})
        their = c.offers.get(partner.id)
        accept_info = None
        if their:
            change = value(a.card, their["give"]) - value(a.card, their["get"])
            can_pay = self._can_pay(a, their["get"])
            accept_info = {"you_give": their["get"], "you_get": their["give"], "wealth_change": change,
                           "you_can_pay": can_pay,
                           "summary": f"{partner.name} offers to give you {fmt_goods(their['give'])} and wants "
                                      f"{fmt_goods(their['get'])} from you. If you accept, your Wealth changes by "
                                      f"{change:+d}" + ("." if can_pay else
                                                        f", but you cannot pay: you lack {self._missing(a, their['get'])}.")}
        ev = {"type": "your_turn", "event_id": f"t-{c.id}-{len(c.messages)}", "chat_id": c.id,
              "partner": partner.name, "transcript": transcript, "their_offer": their, "if_you_accept": accept_info,
              "your_offer": c.offers.get(a.id),
              "messages_left": max(0, self.cfg.messages_per_chat - c.n_msgs),
              "reply_within_s": int(max(0, c.last_message_at + self.cfg.reply_timeout_s - now))}
        ev.update(self._you(a))
        ev.update(self.clock_info(now))
        ev["board"] = self._board_slice(now, self.cfg.board_in_events)
        ev["ledger"] = self._ledger_slice(now, self.cfg.ledger_in_events)
        ev["posts_left_this_minute"] = self._posts_left(a, now)
        ev["notices"] = self._take_notices(a)
        return ev

    def agents_name(self, aid) -> str:
        a = self.agents.get(aid)
        return a.name if a else "?"

    def _market_event(self, a: Agent, now: float) -> dict:
        a.market_seq += 1
        eid = f"m-{a.id}-{self.round['id']}-{a.market_seq}"
        a.market_ids = (a.market_ids + [eid])[-3:]
        chats = []
        for cid in sorted(a.open_chats):
            c = self.chats[cid]
            chats.append({"chat_id": c.id, "partner": self.agents_name(c.other(a.id)),
                          "whose_turn": "you" if c.whose_turn == a.id else "them", "messages": c.n_msgs})
        ev = {"type": "market", "event_id": eid}
        ev.update(self._you(a))
        ev.update(self.clock_info(now))
        ev["agents"] = [o.name for o in self.active_agents() if o.id != a.id]
        ev["dashboard_wealth"] = {r["name"]: r["wealth"] for r in self.wealth_board()}
        ev["open_chats"] = chats
        ev["board"] = self._board_slice(now, self.cfg.board_in_events)
        ev["ledger"] = self._ledger_slice(now, self.cfg.ledger_in_events)
        ev["posts_left_this_minute"] = self._posts_left(a, now)
        ev["house_notice"] = self.house_notice["text"] if self.house_notice else None
        ev["notices"] = self._take_notices(a)
        return ev

    def _game_over_event(self, a: Agent) -> dict:
        me = next((s for s in self.final["standings"] if s["name"] == a.name), None) if self.final else None
        return {"type": "game_over", "event_id": None,
                "rank": me["rank"] if me else None, "of": len(self.final["standings"]) if self.final else 0,
                "wealth": me["wealth"] if me else a.wealth(), "coins": a.coins,
                "winner": self.final["standings"][0]["name"] if self.final and self.final["standings"] else None,
                "standings": [{"rank": s["rank"], "name": s["name"], "wealth": s["wealth"]}
                              for s in (self.final["standings"] if self.final else [])],
                "notices": self._take_notices(a)}

    def pop_event(self, aid: int, now: float | None = None) -> dict | None:
        now = self.clock() if now is None else now
        a = self.agents.get(aid)
        if a is None:
            return None
        a.last_seen = now
        if self.mode in ("over", "reveal") and a.active and not a.game_over_delivered:
            a.game_over_delivered = True
            return self._game_over_event(a)
        if not a.active or not self.market_open():
            return None
        # Oldest turn first, but prefer turns that can still be answered in time: a backlogged bot
        # should not spend a slow LLM call on a chat that will expire before the answer lands.
        best, rushed = None, None
        min_left = min(15.0, self.cfg.reply_timeout_s / 3)
        for cid in a.open_chats:
            c = self.chats[cid]
            if c.whose_turn != a.id:
                continue
            if c.delivered_at is not None and now - c.delivered_at < self.cfg.redeliver_after_s:
                continue
            left = c.last_message_at + self.cfg.reply_timeout_s - now
            if left >= min_left:
                if best is None or c.last_message_at < best[0]:
                    best = (c.last_message_at, c)
            elif rushed is None or c.last_message_at < rushed[0]:
                rushed = (c.last_message_at, c)
        best = best or rushed
        if a.market_due_at is not None and (best is None or a.market_due_at <= best[0]):
            a.market_due_at = None
            return self._market_event(a, now)
        if best is not None:
            best[1].delivered_at = now
            return self._turn_event(a, best[1], now)
        return None

    def wait_event(self, aid: int, now: float | None = None) -> dict:
        now = self.clock() if now is None else now
        a = self.agents.get(aid)
        if a is not None and self.mode in ("over", "reveal") and a.active:
            return self._game_over_event(a)
        ev = {"type": "wait", "event_id": None}
        ev.update(self.clock_info(now))
        if a is not None:
            if not a.active and self.mode == "game":
                ev["info"] = "This game runs without you. Wait for the host to switch back to practice."
            ev["notices"] = self._take_notices(a)
        return ev

    # ------------------------------------------------------------------ acting
    def _can_pay(self, a: Agent, goods: dict) -> bool:
        for k, q in goods.items():
            have = a.coins if k == "coins" else len(a.units.get(k, []))
            if have < q:
                return False
        return True

    def _missing(self, a: Agent, goods: dict) -> str:
        miss = {}
        for k, q in goods.items():
            have = a.coins if k == "coins" else len(a.units.get(k, []))
            if have < q:
                miss[k] = q - have
        return fmt_goods(miss)

    def act(self, aid: int, answer, now: float | None = None) -> dict:
        now = self.clock() if now is None else now
        res = Result()
        a = self.agents.get(aid)
        if a is None:
            res.dropped.append("unknown agent")
            return res.to_dict()
        a.last_seen = now
        if not isinstance(answer, dict):
            res.dropped.append("answer must be a JSON object")
            self._notice(a, "Your last answer was not a JSON object and was ignored.")
            return res.to_dict()
        eid = str(answer.get("event_id") or "")
        if not self.market_open():
            res.dropped.append("the market is closed")
        elif not a.active:
            res.dropped.append("you are not in this market round")
        elif eid.startswith("t-"):
            self._act_turn(a, answer, eid, res, now)
            self._maybe_post(a, answer.get("post"), res, now)
        elif eid.startswith("m-"):
            if eid not in a.market_ids:
                res.dropped.append("stale market event: it was already answered or is too old")
            else:
                a.market_ids.remove(eid)
                self._act_market(a, answer, res, now)
                self._maybe_post(a, answer.get("post"), res, now)
        else:
            res.dropped.append("missing or unknown event_id: copy the event_id of the event you answer")
        for d in res.dropped:
            self._notice(a, f"Dropped: {d}")
        self._log("act", a.id, event_id=eid, applied=res.applied, dropped=res.dropped)
        return res.to_dict()

    def _record(self, c: Chat, author: int | None, text: str, action: str, offer, now: float, system=False):
        m = {"author": author, "text": text, "action": action, "offer": offer, "at": now, "system": system}
        c.messages.append(m)
        if self.store is not None:
            self.store.save_message(c, m)

    def _act_turn(self, a: Agent, answer: dict, eid: str, res: Result, now: float):
        m = re.fullmatch(r"t-(\d+)-(\d+)", eid)
        c = self.chats.get(int(m.group(1))) if m else None
        if c is None or a.id not in (c.a, c.b):
            res.dropped.append("unknown chat in event_id")
            return
        if c.status != "open":
            res.dropped.append(f"chat {c.id} has already ended ({c.end_reason})")
            return
        if c.whose_turn != a.id or len(c.messages) != int(m.group(2)):
            res.dropped.append(f"stale turn for chat {c.id}: it is not your turn or this turn was already answered")
            return
        partner = self.agents[c.other(a.id)]
        raw_action = str(answer.get("action") or "").strip().lower().replace(" ", "_")
        if raw_action in ("post", "board", "post_to_board", "broadcast") and not answer.get("post"):
            answer["post"] = answer.get("message")  # they meant the board: post it, and say it here too
            raw_action = "talk"
        action = ACTION_ALIASES.get(raw_action)
        raw_offer = answer.get("offer")
        has_offer = raw_offer not in (None, "", {}, "null", "none")
        if action is None:
            action = "offer" if has_offer else "talk"
            if raw_action:
                res.dropped.append(f"unknown action '{raw_action}', treated as {action}")
        text = _clean_text(answer.get("message", answer.get("text")))
        if len(text) > self.cfg.message_max_chars:
            text = text[: self.cfg.message_max_chars]
            res.dropped.append(f"message cut to {self.cfg.message_max_chars} characters")
        offer = None
        if action == "offer":
            offer, errs = normalize_offer(raw_offer)
            if offer is None:
                res.dropped.append("offer dropped: " + "; ".join(errs))
                action = "talk"
            elif not self._can_pay(a, offer["give"]):
                res.dropped.append(f"offer dropped: you do not hold {self._missing(a, offer['give'])}")
                offer = None
                action = "talk"
            else:
                if errs:
                    res.dropped.append("parts of your offer were ignored: " + "; ".join(errs))
                c.offers[a.id] = offer
                res.applied.append(f"offer on the table: you give {fmt_goods(offer['give'])}, "
                                   f"you get {fmt_goods(offer['get'])}")
        elif action == "talk" and has_offer:
            res.dropped.append("offer ignored because action was 'talk' (use action 'offer')")
        if action == "accept":
            their = c.offers.get(partner.id)
            if not their:
                res.dropped.append(f"accept dropped: {partner.name} has no offer on the table")
                action = "talk"
            else:
                self._record(c, a.id, text or "Deal.", "accept", None, now)
                ok, why = self._try_deal(c, owner=partner, acceptor=a, offer=their, now=now, res=res)
                if ok:
                    self._end_chat(c, "deal", now, status="deal")
                    return
                self._record(c, None, f"Deal failed: {why}. The chat goes on.", "system", None, now, system=True)
                for side in (a, partner):
                    o = c.offers.get(side.id)
                    if o and not self._can_pay(side, o["give"]):
                        del c.offers[side.id]
                self._notice(partner, f"Deal failed in chat with {a.name}: {why}. The chat goes on.")
                res.dropped.append(f"deal failed: {why}")
                self._pass_turn(c, a, partner, now)
                return
        if action == "end":
            self._record(c, a.id, text or "I'm out.", "end", None, now)
            c.ended_by = a.id
            self._end_chat(c, f"{a.name} walked away", now)
            res.applied.append("chat ended")
            return
        if not text:
            text = "(offer)" if action == "offer" else "..."
        self._record(c, a.id, text, action, offer, now)
        if action == "talk":
            res.applied.append("message sent")
        self._pass_turn(c, a, partner, now)

    def _pass_turn(self, c: Chat, a: Agent, partner: Agent, now: float):
        c.whose_turn = partner.id
        c.last_message_at = now
        c.delivered_at = None
        if c.n_msgs >= self.cfg.messages_per_chat:
            self._end_chat(c, f"message limit ({self.cfg.messages_per_chat}) reached", now)
            return
        if self.store is not None:
            self.store.save_chat(c)
        self._notify(partner.id)

    def _act_market(self, a: Agent, answer: dict, res: Result, now: float):
        opens = answer.get("open")
        if isinstance(opens, dict):
            opens = [opens]
        if opens in (None, "", "null"):
            opens = []
        if not isinstance(opens, list):
            res.dropped.append("'open' must be a list of {to, message, offer}")
            opens = []
        if "action" in answer or "message" in answer:
            res.dropped.append("market answers use 'open' and 'post' only; 'message'/'action' were ignored")
        for o in opens[:25]:
            if isinstance(o, str):
                o = {"to": o}
            if not isinstance(o, dict):
                res.dropped.append("each entry in 'open' must be an object")
                continue
            to = o.get("to", o.get("agent", o.get("partner", o.get("name"))))
            partner = self.resolve_name(to)
            if partner is None:
                res.dropped.append(f"open dropped: no agent named '{to}' in the market")
                continue
            if partner.id == a.id:
                res.dropped.append("open dropped: you cannot chat with yourself")
                continue
            pair = (min(a.id, partner.id), max(a.id, partner.id))
            if pair in self.pair_chat:
                res.dropped.append(f"open dropped: you already have an open chat with {partner.name} "
                                   f"(chat {self.pair_chat[pair]}); reply there when it is your turn")
                continue
            text = _clean_text(o.get("message", o.get("text")))
            if len(text) > self.cfg.message_max_chars:
                text = text[: self.cfg.message_max_chars]
                res.dropped.append(f"message to {partner.name} cut to {self.cfg.message_max_chars} characters")
            offer = None
            raw_offer = o.get("offer")
            if raw_offer not in (None, "", {}, "null", "none"):
                offer, errs = normalize_offer(raw_offer)
                if offer is None:
                    res.dropped.append(f"offer to {partner.name} dropped: " + "; ".join(errs))
                elif not self._can_pay(a, offer["give"]):
                    res.dropped.append(f"offer to {partner.name} dropped: you do not hold "
                                       f"{self._missing(a, offer['give'])}")
                    offer = None
            if not text:
                text = "Hi! Want to trade?"
            c = Chat(id=self.next_chat_id, round_id=self.round["id"], a=a.id, b=partner.id, opened_at=now)
            self.next_chat_id += 1
            self.chats[c.id] = c
            self.open_chat_ids.add(c.id)
            self.pair_chat[pair] = c.id
            a.open_chats.add(c.id)
            partner.open_chats.add(c.id)
            if offer:
                c.offers[a.id] = offer
            self._record(c, a.id, text, "offer" if offer else "talk", offer, now)
            res.applied.append(f"opened chat {c.id} with {partner.name}")
            self._log("open_chat", a.id, chat=c.id, partner=partner.id)
            self._pass_turn(c, a, partner, now)

    def _maybe_post(self, a: Agent, post, res: Result, now: float):
        text = _clean_text(post)
        if not text or text.lower() in ("null", "none"):
            return
        if self._posts_left(a, now) <= 0:
            wait = int(60 - (now - a.post_times[0])) + 1 if a.post_times else 60
            res.dropped.append(f"post dropped: up to {self.cfg.posts_per_minute} posts per minute; "
                               f"next one in {wait} s")
            return
        if len(text) > self.cfg.post_max_chars:
            text = text[: self.cfg.post_max_chars]
            res.dropped.append(f"post cut to {self.cfg.post_max_chars} characters")
        a.post_times.append(now)
        self._add_post(a.id, a.name, text, now)
        res.applied.append("posted to the board")

    def _add_post(self, author_id, author: str, text: str, now: float):
        p = {"id": self.next_post_id, "round_id": self.round["id"] if self.round else 0, "author": author,
             "author_id": author_id, "text": text, "at": now}
        self.next_post_id += 1
        self.posts.append(p)
        if self.store is not None:
            self.store.save_post(p)

    # ------------------------------------------------------------------ deals and chat endings
    def _move(self, frm: Agent, to: Agent, goods: dict, now: float, deal_id: int) -> list[str]:
        moved = []
        for k, q in goods.items():
            if k == "coins":
                frm.coins -= q
                to.coins += q
                continue
            for _ in range(q):
                serial = frm.units[k].pop(0)
                to.units[k].append(serial)
                to.units[k].sort()
                u = self.units.get(serial)
                if u:
                    u["owner"] = to.id
                    u["history"].append([to.id, round(now - self.round["started_at"], 1), deal_id])
                moved.append(serial)
        return moved

    def _try_deal(self, c: Chat, owner: Agent, acceptor: Agent, offer: dict, now: float, res: Result):
        if not self._can_pay(owner, offer["give"]):
            return False, f"{owner.name} no longer holds {self._missing(owner, offer['give'])}"
        if not self._can_pay(acceptor, offer["get"]):
            return False, f"{acceptor.name} cannot pay {self._missing(acceptor, offer['get'])}"
        did = self.next_deal_id
        self.next_deal_id += 1
        a_serials = self._move(owner, acceptor, offer["give"], now, did)
        b_serials = self._move(acceptor, owner, offer["get"], now, did)
        g, r = offer["give"], offer["get"]
        if set(g) == {"coins"} and "coins" not in r:
            text = f"{acceptor.name} sold {fmt_goods(r)} to {owner.name} for {g['coins']} coins"
            seller, buyer = acceptor, owner
        elif set(r) == {"coins"} and "coins" not in g:
            text = f"{owner.name} sold {fmt_goods(g)} to {acceptor.name} for {r['coins']} coins"
            seller, buyer = owner, acceptor
        else:
            text = f"{owner.name} traded {fmt_goods(g)} to {acceptor.name} for {fmt_goods(r)}"
            seller, buyer = owner, acceptor
        d = {"id": did, "round_id": self.round["id"], "chat_id": c.id, "at": now, "a_id": owner.id,
             "b_id": acceptor.id, "a": owner.name, "b": acceptor.name, "a_gives": g, "b_gives": r,
             "a_serials": a_serials, "b_serials": b_serials, "seller": seller.name, "buyer": buyer.name,
             "text": text}
        self.deals.append(d)
        t = round(now - self.round["started_at"], 1)
        for side in (owner, acceptor):
            side.series.append([t, side.wealth()])
        if self.store is not None:
            self.store.save_deal(d)
            self.store.save_agent(owner)
            self.store.save_agent(acceptor)
        self._log("deal", None, **{k: v for k, v in d.items() if k != "text"})
        self._notice(owner, f"Deal with {acceptor.name}: you gave {fmt_goods(g)}, you got {fmt_goods(r)}. "
                            f"Your Wealth is now {owner.wealth()}.")
        self._notice(acceptor, f"Deal with {owner.name}: you gave {fmt_goods(r)}, you got {fmt_goods(g)}. "
                               f"Your Wealth is now {acceptor.wealth()}.")
        res.applied.append(f"deal: you gave {fmt_goods(r)}, you got {fmt_goods(g)}")
        res.deal = {"id": did, "text": text, "your_wealth": acceptor.wealth()}
        return True, ""

    def _end_chat(self, c: Chat, reason: str, now: float, status: str = "ended"):
        if c.status != "open":
            return
        c.status = status
        c.end_reason = reason
        c.ended_at = now
        self.open_chat_ids.discard(c.id)
        self.pair_chat.pop((min(c.a, c.b), max(c.a, c.b)), None)
        for aid in (c.a, c.b):
            ag = self.agents.get(aid)
            if ag:
                ag.open_chats.discard(c.id)
                if status != "deal":
                    other = self.agents_name(c.other(aid))
                    self._notice(ag, f"Chat {c.id} with {other} ended: {reason}.")
                self._notify(aid)
        self.recent_ended.append(c.id)
        if self.store is not None:
            self.store.save_chat(c)
        self._log("chat_end", None, chat=c.id, reason=reason, status=status)

    # ------------------------------------------------------------------ bell, standings, fun facts
    def _bell(self, now: float):
        for cid in list(self.open_chat_ids):
            self._end_chat(self.chats[cid], "the bell rang", now)
        self.round["ends_at"] = min(self.round["ends_at"], now)
        standings = []
        for a in self.active_agents():
            standings.append({"id": a.id, "name": a.name, "house": a.is_house, "wealth": a.wealth(),
                              "coins": a.coins, "items": a.items(), "card": dict(a.card),
                              "gain": a.wealth() - a.start_wealth, "flip": self.rng.random()})
        standings.sort(key=lambda s: (-s["wealth"], -s["coins"], s["flip"]))
        for i, s in enumerate(standings):
            s["rank"] = i + 1
            s["coin_flip"] = any(o is not s and o["wealth"] == s["wealth"] and o["coins"] == s["coins"]
                                 for o in standings)
            del s["flip"]
        self.final = {"round": self.round["id"], "ended_at": now, "standings": standings,
                      "fun_facts": self.fun_facts(), "deals": len(self.deals), "posts": len(self.posts)}
        self.mode = "over"
        if self.store is not None:
            self.store.end_round(self.round["id"], now, self.final)
            self.store.update_round_end(self.round["id"], self.round["ends_at"])
        self._log("bell", None, round=self.round["id"])
        for a in self.active_agents():
            a.game_over_delivered = False
            self._notify(a.id)

    def fun_facts(self) -> dict:
        facts: dict = {}
        best = None
        for d in self.deals:
            a, b = self.agents.get(d["a_id"]), self.agents.get(d["b_id"])
            if not a or not b:
                continue
            ga = value(a.card, d["b_gives"]) - value(a.card, d["a_gives"])
            gb = value(b.card, d["a_gives"]) - value(b.card, d["b_gives"])
            for who, gain in ((a.name, ga), (b.name, gb)):
                if best is None or gain > best["gain"]:
                    best = {"agent": who, "gain": gain, "deal": d["text"]}
        if best:
            facts["best_deal"] = best
        counts: dict = {}
        for c in self.chats.values():
            if c.round_id != self.round["id"]:
                continue
            for aid in (c.a, c.b):
                counts[aid] = counts.get(aid, 0) + 1
        if counts:
            aid = max(counts, key=counts.get)
            facts["most_chats"] = {"agent": self.agents_name(aid), "chats": counts[aid]}
        post = self._best_board_post()
        if post:
            facts["best_board_post"] = post
        walk = None
        for c in self.chats.values():
            if c.round_id != self.round["id"] or c.ended_by is None:
                continue
            offers = list(c.offers.values())
            gap = 0
            if len(offers) == 2:
                gap = abs(offers[0]["give"].get("coins", 0) + offers[0]["get"].get("coins", 0)
                          - offers[1]["give"].get("coins", 0) - offers[1]["get"].get("coins", 0))
            score = c.n_msgs * 10 + gap / 10
            if walk is None or score > walk["score"]:
                last = next((m["text"] for m in reversed(c.messages) if m["author"] == c.ended_by), "")
                walk = {"score": score, "agent": self.agents_name(c.ended_by),
                        "partner": self.agents_name(c.other(c.ended_by)), "messages": c.n_msgs, "last_words": last}
        if walk:
            walk.pop("score")
            facts["most_dramatic_walkout"] = walk
        return facts

    def _best_board_post(self):
        """The board post that paid off most: its author's Wealth gain from deals in the 3 minutes after
        it. True or not is not judged; bluffing is allowed."""
        best = None
        for p in self.posts:
            aid = p.get("author_id")
            if aid is None or aid not in self.agents:
                continue
            a = self.agents[aid]
            gain = 0
            for d in self.deals:
                if p["at"] <= d["at"] <= p["at"] + 180 and aid in (d["a_id"], d["b_id"]):
                    mine, theirs = (d["a_gives"], d["b_gives"]) if aid == d["a_id"] else (d["b_gives"], d["a_gives"])
                    gain += value(a.card, theirs) - value(a.card, mine)
            if gain > 0 and (best is None or gain > best["gain"]):
                best = {"agent": a.name, "post": p["text"], "gain": gain}
        return best

    # ------------------------------------------------------------------ public views
    def board(self, since: int = 0, limit: int = 500) -> dict:
        now = self.clock()
        posts = [p for p in self.posts if p["id"] > since][-limit:]
        return {"round": self.round["id"] if self.round else None, "mode": self.mode,
                "posts": [{"id": p["id"], "author": p["author"], "text": p["text"], "ago_s": int(now - p["at"])}
                          for p in posts],
                "next_since": posts[-1]["id"] if posts else since}

    def _chat_view(self, c: Chat, n_msgs: int = 6) -> dict:
        return {"id": c.id, "a": self.agents_name(c.a), "b": self.agents_name(c.b), "status": c.status,
                "end_reason": c.end_reason, "n": c.n_msgs,
                "stamp": "DEAL" if c.status == "deal" else ("END" if c.status == "ended" else None),
                "offers": {self.agents_name(k): fmt_offer(v) for k, v in c.offers.items()},
                "whose_turn": self.agents_name(c.whose_turn) if c.status == "open" else None,
                "messages": [{"from": "SERVER" if m.get("system") else self.agents_name(m["author"]),
                              "text": m["text"], "action": m["action"], "offer": fmt_offer(m.get("offer"))}
                             for m in c.messages[-n_msgs:]],
                "last_at": c.last_message_at}

    def _round_chats(self) -> list[Chat]:
        rid = self.round["id"] if self.round else None
        return [c for c in self.chats.values() if c.round_id == rid]

    def wealth_board(self) -> list[dict]:
        rows = [{"name": a.name, "wealth": a.wealth(), "gain": a.wealth() - a.start_wealth, "house": a.is_house}
                for a in self.active_agents()]
        rows.sort(key=lambda r: -r["wealth"])
        return rows

    def dashboard(self) -> dict:
        """The public dashboard, for every human and every agent: Wealth, deals, board, and who is
        chatting with whom. Never the words of a chat (agents read only their own chats)."""
        now = self.clock()
        started = self.round["started_at"] if self.round else now
        agents = []
        deal_count = {}
        for d in self.deals:
            for aid in (d["a_id"], d["b_id"]):
                deal_count[aid] = deal_count.get(aid, 0) + 1
        for a in sorted(self.active_agents(), key=lambda x: x.id):
            agents.append({"id": a.id, "name": a.name, "house": a.is_house, "wealth": a.wealth(),
                           "gain": a.wealth() - a.start_wealth, "series": a.series, "open_chats": len(a.open_chats),
                           "deals": deal_count.get(a.id, 0)})
        chats = sorted(self._round_chats(), key=lambda c: (c.status != "open", -c.last_message_at))
        st = {"mode": self.mode, "now": round(now - started, 1), "clock": self.clock_info(now),
              "registration_open": self.registration_open, "house_notice": self.house_notice,
              "length_s": (self.round["ends_at"] - started) if self.round and self.round.get("ends_at")
              else self.cfg.practice_reset_minutes * 60,
              "agents": agents,
              "links": [[self.chats[cid].a, self.chats[cid].b, self.chats[cid].n_msgs] for cid in self.open_chat_ids],
              "chats": [{"id": c.id, "a": self.agents_name(c.a), "b": self.agents_name(c.b), "status": c.status,
                         "n": c.n_msgs, "end_reason": c.end_reason, "ago_s": int(now - c.last_message_at)}
                        for c in chats[:300]],
              "board": self.board(limit=30)["posts"][::-1],
              "ledger": [{"text": d["text"], "ago_s": int(now - d["at"])} for d in self.deals[-30:]][::-1],
              "stats": {"deals": len(self.deals), "posts": len(self.posts), "open_chats": len(self.open_chat_ids),
                        "chats_total": len(chats)},
              "practice": list(self.practice_history)[-6:]}
        if self.mode in ("over", "reveal") and self.final:
            st["final"] = {"standings": [{k: s[k] for k in ("rank", "name", "wealth", "house")}
                                         for s in self.final["standings"]]}
            if self.mode == "reveal":
                st["final"] = self.final
        return st

    def watch_chats(self, agent: str | None = None) -> list[dict]:
        """For human spectators: every chat of the round with its pinned offers and last message."""
        now = self.clock()
        out = []
        for c in sorted(self._round_chats(), key=lambda c: (c.status != "open", -c.last_message_at)):
            names = (self.agents_name(c.a), self.agents_name(c.b))
            if agent and agent not in names:
                continue
            v = self._chat_view(c, n_msgs=1)
            v["last"] = v.pop("messages")[-1] if c.messages else None
            v["ago_s"] = int(now - c.last_message_at)
            out.append(v)
        return out

    def watch_chat(self, chat_id: int) -> dict | None:
        c = self.chats.get(chat_id)
        if c is None:
            return None
        v = self._chat_view(c, n_msgs=10_000)
        v["ago_s"] = int(self.clock() - c.last_message_at)
        return v

    def team_log(self, a: Agent) -> dict:
        now = self.clock()
        rounds = list(self.archive)
        if self.round:
            rounds.append({"round": self.round["id"], "mode": self.round["mode"],
                           "chats": [c for c in self.chats.values() if c.round_id == self.round["id"]],
                           "deals": self.deals, "posts": self.posts})
        seen = set()
        chats, deals, posts = [], [], []
        for r in rounds:
            for c in r["chats"]:
                if a.id in (c.a, c.b) and c.id not in seen:
                    seen.add(c.id)
                    v = self._chat_view(c, n_msgs=200)
                    v["round"] = r["round"]
                    chats.append(v)
            deals += [dict(round=r["round"], text=d["text"], at_s=round(d["at"] - (self.round or {}).get(
                "started_at", 0), 1)) for d in r["deals"] if a.id in (d["a_id"], d["b_id"])]
            posts += [{"round": r["round"], "text": p["text"]} for p in r["posts"] if p.get("author_id") == a.id]
        gains = [{"round": h["round"], "gain": h["gains"].get(a.name)} for h in self.practice_history
                 if a.name in h["gains"]]
        out = {"team": a.name, "mode": self.mode, "clock": self.clock_info(now), "active": a.active,
               "wealth": a.wealth() if a.active else None, "gain_this_round": a.wealth() - a.start_wealth
               if a.active else None, "holdings": a.holdings() if a.active else None,
               "card": a.card if a.active else None, "gain_per_reset": gains,
               "chats": chats[-25:], "deals": deals[-50:], "posts": posts[-50:],
               "notices": list(a.recent_notices)[-30:]}
        return out

    def host_state(self) -> dict:
        now = self.clock()
        return {"mode": self.mode, "clock": self.clock_info(now), "registration_open": self.registration_open,
                "config": self.cfg.to_dict(), "house_notice": self.house_notice,
                "teams": [{"id": a.id, "name": a.name, "key": a.key, "house": a.is_house, "active": a.active,
                           "wealth": a.wealth() if a.active else None,
                           "last_seen_s": int(now - a.last_seen) if a.last_seen else None,
                           "open_chats": len(a.open_chats), "requests": a.requests}
                          for a in sorted(self.agents.values(), key=lambda x: (x.is_house, x.id))],
                "stats": {"deals": len(self.deals), "posts": len(self.posts), "open_chats": len(self.open_chat_ids)},
                "final": self.final}

    def export(self) -> dict:
        """Everything about the current (or just finished) round, for the debrief."""
        rid = self.round["id"] if self.round else None
        chats = [c for c in self.chats.values() if c.round_id == rid]
        return {"round": rid, "mode": self.mode,
                "agents": [{"name": a.name, "house": a.is_house, "card": a.card, "start_coins": a.start_coins,
                            "start_wealth": a.start_wealth, "coins": a.coins, "items": a.items(),
                            "wealth": a.wealth()} for a in self.active_agents()],
                "chats": [self._chat_view(c, n_msgs=10_000) for c in chats],
                "deals": [{k: v for k, v in d.items()} for d in self.deals],
                "posts": [{"author": p["author"], "text": p["text"], "at": p["at"]} for p in self.posts],
                "units": self.units, "final": self.final, "practice_history": list(self.practice_history)}

    def transcripts_text(self) -> str:
        ex = self.export()
        lines = [f"# The Espresso Exchange: transcripts, round {ex['round']} ({ex['mode']})", ""]
        if ex["final"]:
            lines.append("## Final standings")
            for s in ex["final"]["standings"]:
                card = ", ".join(f"{k} {v}" for k, v in sorted(s["card"].items(), key=lambda kv: -kv[1]))
                lines.append(f"{s['rank']}. {s['name']}: Wealth {s['wealth']} ({s['coins']} coins; card: {card})")
            lines.append("")
        lines.append("## Ledger")
        lines += [f"- {d['text']}" for d in ex["deals"]] or ["- no deals"]
        lines += ["", "## Board"]
        lines += [f"- {p['author']}: {p['text']}" for p in ex["posts"]] or ["- no posts"]
        lines += ["", "## Chats"]
        for c in ex["chats"]:
            lines.append(f"### Chat {c['id']}: {c['a']} and {c['b']} ({c['status']}: {c['end_reason'] or 'open'})")
            for m in c["messages"]:
                extra = f"  [offer: {m['offer']}]" if m["offer"] else ""
                lines.append(f"- {m['from']} ({m['action']}): {m['text']}{extra}")
            lines.append("")
        return "\n".join(lines)

    # ------------------------------------------------------------------ restore after a restart
    def restore(self):
        """Reload teams; resume a game round that was running; otherwise open a practice round."""
        if self.store is None:
            return False
        for row in self.store.load_agents():
            name = row["name"]
            if row["is_house"] and name in LEGACY_HOUSE_NAMES:
                name = LEGACY_HOUSE_NAMES[name]
                self.store.db.execute("UPDATE agents SET name=? WHERE id=?", (name, row["id"]))
            a = Agent(id=row["id"], name=name, key=row["key"], is_house=bool(row["is_house"]),
                      created_at=row["created_at"] or 0.0)
            self.agents[a.id] = a
            self.by_key[a.key] = a.id
            self.by_name[a.name.lower()] = a.id
        last = self.store.last_round()
        now = self.clock()
        if last and last["mode"] == "game" and last["ended_at"] is None and (last["ends_at"] or 0) > now:
            self._restore_game(last, now)
            return True
        return False

    def _restore_game(self, last: dict, now: float):
        setup = json.loads(last["setup"])
        self.round_seq = last["id"]
        self.mode = "game"
        self.registration_open = False
        self.round = {"id": last["id"], "mode": "game", "started_at": last["started_at"], "ends_at": last["ends_at"],
                      "perm": setup["perm"], "cols": setup["cols"], "next_serial": setup["next_serial"],
                      "latin_k": len(setup["agents"]), "last_call_done":
                          now >= last["started_at"] + self.cfg.last_call_minutes * 60, "reset_at": None}
        self.units = {}
        for a in self.agents.values():
            self._reset_agent_round_state(a, now)
            s = setup["agents"].get(str(a.id))
            a.active = s is not None
            a.units = {t: [] for t in ITEMS}
            if not s:
                a.coins, a.card = 0, {}
                continue
            a.coins, a.card, a.start_wealth = s["coins"], s["card"], s["start_wealth"]
            a.start_coins = s["coins"]
            for t in ITEMS:
                a.units[t] = list(s["units"][t])
                for serial in a.units[t]:
                    self.units[serial] = {"type": t, "owner": a.id, "history": [[a.id, 0.0, None]]}
            a.series = [[0.0, a.wealth()]]
        self.deals = []
        for d in self.store.round_deals(last["id"]):
            owner, acc = self.agents.get(d["a_id"]), self.agents.get(d["b_id"])
            if not owner or not acc:
                continue
            for frm, to, goods, serials in ((owner, acc, d["a_gives"], d["a_serials"]),
                                            (acc, owner, d["b_gives"], d["b_serials"])):
                frm.coins -= goods.get("coins", 0)
                to.coins += goods.get("coins", 0)
                for serial in serials:
                    t = self.units[serial]["type"]
                    if serial in frm.units[t]:
                        frm.units[t].remove(serial)
                    to.units[t].append(serial)
                    to.units[t].sort()
                    self.units[serial]["owner"] = to.id
                    self.units[serial]["history"].append([to.id, round(d["at"] - last["started_at"], 1), d["id"]])
            t_rel = round(d["at"] - last["started_at"], 1)
            owner.series.append([t_rel, owner.wealth()])
            acc.series.append([t_rel, acc.wealth()])
            self.deals.append(d)
        self.posts = [{"id": p["id"], "round_id": p["round_id"], "author": p["author"], "author_id": p["author_id"],
                       "text": p["text"], "at": p["at"]} for p in self.store.round_posts(last["id"])]
        msgs: dict = {}
        for m in self.store.round_messages(last["id"]):
            msgs.setdefault(m["chat_id"], []).append(m)
        for row in self.store.round_chats(last["id"]):
            c = Chat(id=row["id"], round_id=row["round_id"], a=row["agent_a"], b=row["agent_b"],
                     opened_at=row["opened_at"] or 0.0, status=row["status"] if row["status"] != "open" else "ended",
                     whose_turn=row["whose_turn"] or 0, last_message_at=row["last_message_at"] or 0.0,
                     end_reason=row["end_reason"] or "server restart", ended_at=row["ended_at"] or now)
            c.messages = [{"author": m["author"], "text": m["text"], "action": m["action"], "offer": m["offer"],
                           "at": m["at"], "system": bool(m["system"])} for m in msgs.get(c.id, [])]
            self.chats[c.id] = c
            if self.store is not None and row["status"] == "open":
                self.store.save_chat(c)
        for a in self.active_agents():
            a.market_due_at = now
            self._notice(a, "The server restarted. Your holdings are restored; open chats were closed.")
        self.next_market_tick = now + self.cfg.market_interval_s
        self._log("restore", None, round=last["id"], deals=len(self.deals))
