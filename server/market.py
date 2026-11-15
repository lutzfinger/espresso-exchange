"""The Espresso Exchange market engine.

Synchronous and single-threaded by design: the web layer calls these methods from one
asyncio event loop, so every act() runs to completion before the next one starts. That is
the "one queue" from the spec: two deals can never spend the same coin.

Version 2 rules (Config.v2_rules) add item multipliers moved by news, private tips, house buy
orders, a price ticker, a trade fee, shell companies with private transfers, audits, multi-party
contracts, a set bonus and a leader pulse. v2_rules=False plays the classic game.
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
from .items import ITEMS, SERIAL_PREFIX, fmt_goods, fmt_offer, norm_goods, normalize_offer, value
from .rules_v2 import (BASE_VALUE, MULT_MAX, MULT_MIN, clean_person, contract_errors, fmt_factor, plan_news,
                       plan_orders, split_members, ticker_print, ticker_view, tip_text)

HOUSE_NAMES = ["Fair Trader", "Hardliner", "Wildcard", "Opener"]
WATCH_ENDED_MAX = 150  # finished chats per /api/watch/chats answer (a 25-minute game had 7,600 chats = 3 MB)
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
EXTRA_KEYS = ("create_shell", "transfer", "house_ask", "contract_reply", "contract", "audit")
MEMBERS_HELP = ('List every team member\'s first and last name, for example {"team": "Crema Cartel", '
                '"members": ["Ana Lopez", "Ben Wu"]}. Each person can be on one team only.')
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


def _given(x) -> bool:
    """An answer field the bot actually filled in (LLMs write null, "none", {} or [] for nothing)."""
    if x is None or x == {} or x == [] or x is False:
        return False
    return not (isinstance(x, str) and x.strip().lower() in ("", "null", "none", "no", "false"))


def _int(x):
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return int(round(f)) if abs(f) < 1e9 else None


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
    members: list = field(default_factory=list)   # the team's people (host only)
    owner: int | None = None                      # set for a shell company: the id of its team
    alerts: list = field(default_factory=list)    # why the next market event was sent early
    tips: list = field(default_factory=list)      # private tips received this round

    def items(self) -> dict:
        return {t: len(self.units[t]) for t in ITEMS}

    def wealth(self) -> int:
        """Classic Wealth at card values (multipliers 1). The market's W() is the one that counts."""
        return self.coins + sum(self.card.get(t, 0) * len(self.units[t]) for t in ITEMS)

    def holdings(self) -> dict:
        return {"coins": self.coins, "items": self.items()}

    def holds_anything(self) -> bool:
        return self.coins > 0 or any(self.units[t] for t in ITEMS)


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
        # version 2 state
        self.shells_of: dict[int, list[int]] = {}      # team id -> its shell ids
        self.member_keys: dict[str, int] = {}          # normalized person name -> team id
        self.mult: dict[str, float] = {t: 1.0 for t in ITEMS}
        self.prints: dict[str, list] = {t: [] for t in ITEMS}
        self.news_log: list[dict] = []
        self.orders: list[dict] = []
        self.contracts: dict[int, dict] = {}
        self.next_contract_id = 1
        self.private: list[dict] = []                  # transfers and audits of this round (never public)
        self.pulse: dict | None = None
        self.move_seq = 0
        if store is not None:
            self.next_agent_id = store.max_id("agents") + 1
            self.next_chat_id = store.max_id("chats") + 1
            self.next_deal_id = store.max_id("deals") + 1
            self.next_post_id = store.max_id("posts") + 1
            self.round_seq = store.max_id("rounds")
            self.removed_names = store.removed_names()
            self.key_epoch = int(store.get_meta("key_epoch", 0))

    # ------------------------------------------------------------------ helpers
    @property
    def v2(self) -> bool:
        return bool(self.cfg.v2_rules)

    def fee(self) -> int:
        return int(self.cfg.trade_fee) if self.v2 else 0

    def wealth_public(self) -> bool:
        return (not self.v2) or bool(self.cfg.public_wealth)

    def _seq(self) -> int:
        self.move_seq += 1
        return self.move_seq

    def _log(self, kind, agent=None, **data):
        if self.store is not None:
            self.store.log(kind, agent, **data)

    def _notify(self, aid: int):
        try:
            self.on_ready(self.team_id(aid))
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
        """Registered teams: not the house bots and not the shell companies."""
        return [a for a in self.agents.values() if not a.is_house and a.owner is None]

    def market_open(self) -> bool:
        return self.round is not None and self.mode in ("practice", "game")

    # ---- teams and their entities (a team = its main agent plus its shell companies)
    def team_id(self, aid) -> int:
        a = self.agents.get(aid)
        return a.owner if a is not None and a.owner is not None else aid

    def entities(self, tid: int) -> list[Agent]:
        main = self.agents.get(tid)
        if main is None:
            return []
        return [main] + [self.agents[s] for s in self.shells_of.get(tid, []) if s in self.agents]

    def _label(self, a: Agent | None) -> str:
        if a is None:
            return "?"
        if a.owner is None:
            return a.name
        return f"{a.name} (shell of {self.agents_name(a.owner)})"

    # ---- values
    def values_now(self, a: Agent) -> dict:
        if not a.card:
            return {}
        if not self.v2:
            return dict(a.card)
        return {t: int(round(a.card.get(t, 0) * self.mult.get(t, 1.0))) for t in ITEMS}

    def W(self, a: Agent) -> int:
        """Wealth: coins + items at today's values (card x multiplier)."""
        v = self.values_now(a)
        return a.coins + sum(v.get(t, 0) * len(a.units[t]) for t in ITEMS)

    def set_complete(self, tid: int) -> bool:
        ents = self.entities(tid)
        return bool(ents) and all(any(e.units[t] for e in ents) for t in ITEMS)

    def set_bonus(self, tid: int) -> int:
        return int(self.cfg.set_bonus) if self.v2 and self.set_complete(tid) else 0

    def team_score(self, tid: int) -> int:
        return sum(self.W(e) for e in self.entities(tid)) + self.set_bonus(tid)

    def _point(self, a: Agent, now: float):
        if self.round is not None:
            a.series.append([round(now - self.round["started_at"], 1), self.W(a)])

    def _wants_market(self, a: Agent) -> bool:
        """Shell companies get market events only while they hold something."""
        return a.owner is None or a.holds_anything()

    def _wake(self, a: Agent, reason: str, force: bool = False):
        """Send an early market event: news, a tip, a house order, the pulse or a contract."""
        if not a.active or not (force or self._wants_market(a)):
            return
        a.alerts = (a.alerts + [reason])[-8:]
        if a.market_due_at is None:
            a.market_due_at = self.clock()
        self._notify(a.id)

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

    def clean_members(self, raw) -> list[tuple[str, str]]:
        """[(display, key)] for the team's people. One team per person: the key is accent- and case-free."""
        parts = split_members(raw)
        if not parts:
            if self.cfg.require_members:
                raise GameError(MEMBERS_HELP)
            return []
        if len(parts) > self.cfg.max_members:
            raise GameError(f"At most {self.cfg.max_members} members per team.")
        out, seen = [], set()
        for p in parts:
            try:
                disp, key = clean_person(p)
            except ValueError as e:
                raise GameError(f"Member {e}.") from None
            if key in seen:
                raise GameError(f"'{disp}' is listed twice.")
            if key in self.member_keys:
                raise GameError(f"'{disp}' is already on a team. Each person can be on one team only; "
                                f"ask the host if this is a mistake.")
            seen.add(key)
            out.append((disp, key))
        return out

    def register(self, raw_name, is_house: bool = False, members=None, recover: bool = False) -> Agent:
        name = raw_name if is_house else self.clean_name(raw_name)
        people: list = []
        if not is_house:
            if not self.registration_open:
                raise GameError("Registration is closed while the game runs.")
            if name.lower() in self.by_name:
                raise GameError(f"The name '{name}' is taken. If it is yours, ask the host for your key.")
            if name.lower() in self.removed_names:
                raise GameError(f"The name '{name}' was removed by the host.")
            if len(self.teams()) >= self.cfg.max_teams:
                raise GameError("The market is full.")
            if not recover:
                people = self.clean_members(members)
        key = self.make_key(name) if not is_house else "house_" + secrets.token_urlsafe(18)
        a = Agent(id=self.next_agent_id, name=name, key=key, is_house=is_house, created_at=self.clock(),
                  members=[p[0] for p in people])
        self.next_agent_id += 1
        self.agents[a.id] = a
        self.by_key[key] = a.id
        self.by_name[name.lower()] = a.id
        for _, k in people:
            self.member_keys[k] = a.id
        if self.store is not None:
            self.store.save_agent(a)
        self._log("register", a.id, name=name, house=is_house, members=len(people))
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
                return self.register(name, recover=True)
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

    def create_shell(self, owner: Agent, raw_name) -> Agent:
        """A shell company for a team: secret owner, starts every round empty, plays through the team's key."""
        if not self.v2 or self.cfg.shells_per_team <= 0:
            raise GameError("Shell companies are not part of this game.")
        main = self.agents[owner.owner] if owner.owner is not None else owner
        if main.is_house:
            raise GameError("House bots have no shell companies.")
        if len(self.shells_of.get(main.id, [])) >= self.cfg.shells_per_team:
            raise GameError(f"Your team already has {self.cfg.shells_per_team} shell companies, the most allowed.")
        name = self.clean_name(raw_name)
        if name.lower() in self.by_name:
            raise GameError(f"The name '{name}' is taken. Pick another for your shell company.")
        if name.lower() in self.removed_names:
            raise GameError(f"The name '{name}' was removed by the host.")
        s = Agent(id=self.next_agent_id, name=name, key="", owner=main.id, created_at=self.clock())
        self.next_agent_id += 1
        self.agents[s.id] = s
        self.by_name[name.lower()] = s.id
        self.shells_of.setdefault(main.id, []).append(s.id)
        if self.store is not None:
            self.store.save_agent(s)
        self._log("shell", s.id, name=name, team=main.id)
        if self.round is not None and main.active and self.market_open():
            self._join_shell(s, self.clock())
        return s

    def _drop_agent(self, a: Agent, reason: str):
        now = self.clock()
        for cid in list(a.open_chats):
            self._end_chat(self.chats[cid], reason, now)
        for c in self.contracts.values():
            if c["status"] == "open" and a.id in c["parties"]:
                self._end_contract(c, "cancelled", f"{a.name} left the market", now)
        a.active = False
        if a.key:
            self.by_key.pop(a.key, None)
        self.by_name.pop(a.name.lower(), None)
        self.removed_names.add(a.name.lower())
        for k in [k for k, tid in self.member_keys.items() if tid == a.id]:
            del self.member_keys[k]
        if a.owner is not None and a.id in self.shells_of.get(a.owner, []):
            self.shells_of[a.owner].remove(a.id)
        self.shells_of.pop(a.id, None)
        del self.agents[a.id]
        if self.store is not None:
            self.store.remove_agent(a.id)
        self._log("remove", a.id, name=a.name)

    def wipe_teams(self) -> int:
        """Fresh start before an event: every team is gone and every old key stops working."""
        now = self.clock()
        n = 0
        for a in [x for x in self.agents.values() if not x.is_house]:
            for cid in list(a.open_chats):
                self._end_chat(self.chats[cid], "the host wiped all teams", now)
            if a.key:
                self.by_key.pop(a.key, None)
            self.by_name.pop(a.name.lower(), None)
            del self.agents[a.id]
            n += a.owner is None
        self.shells_of.clear()
        self.member_keys.clear()
        self.contracts.clear()
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
        """Exact name only (a shell's name removes just that shell; a team's name removes it and its shells)."""
        a = self.agents.get(self.by_name.get(re.sub(r"\s+", " ", str(name or "")).strip().lower(), -1))
        if a is None or a.is_house:
            return False
        if a.owner is None:
            for sid in list(self.shells_of.get(a.id, [])):
                if sid in self.agents:
                    self._drop_agent(self.agents[sid], "the host removed a team")
        self._drop_agent(a, "the host removed a team")
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
        a.alerts = []
        a.tips = []

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
        a.start_wealth = self.W(a)
        a.start_coins = a.coins
        a.series = [[round(now - self.round["started_at"], 1), self.W(a)]]
        a.market_due_at = now
        self._log("join_round", a.id, round=self.round["id"], coins=a.coins, items=a.items(), card=a.card)
        self._notify(a.id)

    def _join_shell(self, s: Agent, now: float):
        owner = self.agents[s.owner]
        self._reset_agent_round_state(s, now)
        s.active = True
        s.card = dict(owner.card)
        s.units = {t: [] for t in ITEMS}
        s.coins = 0
        s.start_wealth = 0
        s.start_coins = 0
        s.series = [[round(now - self.round["started_at"], 1), 0]]

    def _setup_snapshot(self) -> dict:
        return {"agents": {str(a.id): {"name": a.name, "coins": a.coins, "card": a.card,
                                       "units": {t: list(a.units[t]) for t in ITEMS}, "start_wealth": a.start_wealth}
                           for a in self.active_agents()},
                "perm": self.round["perm"], "cols": self.round["cols"],
                "next_serial": self.round["next_serial"],
                "news": self.round.get("news", []), "orders": self.round.get("orders_plan", [])}

    def round_length_s(self, mode: str) -> float:
        return (self.cfg.game_minutes if mode == "game" else self.cfg.practice_reset_minutes) * 60

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
                      "reset_at": now + self.cfg.practice_reset_minutes * 60 if mode == "practice" else None,
                      "news": [], "orders_plan": [], "pulse_next": None}
        self.units = {}
        self.deals = []
        self.posts = []
        self.recent_ended = deque(maxlen=12)
        self.mult = {t: 1.0 for t in ITEMS}
        self.prints = {t: [] for t in ITEMS}
        self.news_log = []
        self.orders = []
        self.contracts = {}
        self.private = []
        self.pulse = None
        mains = [a for a in self.agents.values() if a.owner is None and
                 ((not a.is_house) or mode == "practice" or self.cfg.house_in_game)]
        main_ids = {a.id for a in mains}
        shells = [a for a in self.agents.values() if a.owner is not None and a.owner in main_ids]
        shell_ids = {a.id for a in shells}
        for a in self.agents.values():
            self._reset_agent_round_state(a, now)
            a.active = a.id in main_ids or a.id in shell_ids
            if not a.active:
                a.units = {t: [] for t in ITEMS}
                a.coins = 0
                a.card = {}
        self._deal(mains, now)
        for s in shells:
            self._join_shell(s, now)
        if self.v2:
            length = self.round_length_s(mode)
            n = self.cfg.news_count
            if mode == "practice" and n:
                n = max(1, round(n * length / max(60.0, self.cfg.game_minutes * 60)))
            tippable = [a.id for a in mains if not a.is_house]
            self.round["news"] = plan_news(self.rng, length, n, self.cfg.tip_lead_s, tippable,
                                           self.cfg.tips_per_news, self.cfg.tip_accuracy)
            until = min(length, self.cfg.last_call_minutes * 60) if mode == "game" else length
            self.round["orders_plan"] = plan_orders(self.rng, self.cfg.house_order_first_s,
                                                    self.cfg.house_order_every_s, self.cfg.house_order_window_s,
                                                    until, self.cfg.house_order_qty)
            self.round["pulse_next"] = self.cfg.pulse_every_s if self.cfg.pulse_every_s > 0 else None
        for a in mains:
            a.series = [[0.0, self.W(a)]]
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
        self._log("round_start", None, round=self.round["id"], mode=mode,
                  agents=[a.id for a in self.active_agents()])
        for a in mains:
            self._notify(a.id)

    def _close_round(self, now: float, reason: str):
        if self.round is None:
            return
        for cid in list(self.open_chat_ids):
            self._end_chat(self.chats[cid], reason, now)
        for c in list(self.contracts.values()):
            if c["status"] == "open":
                self._end_contract(c, "expired", reason, now)
        results = None
        if self.round["mode"] == "practice":
            mains = [a for a in self.active_agents() if a.owner is None]
            gains = {a.name: self.team_score(a.id) - a.start_wealth for a in mains}
            wealth = {a.name: self.team_score(a.id) for a in mains}
            entry = {"round": self.round["id"], "started_at": self.round["started_at"], "ended_at": now,
                     "gains": gains, "wealth": wealth, "deals": len(self.deals)}
            self.practice_history.append(entry)
            results = entry
        elif self.final:
            results = self.final
        self.archive.append({"round": self.round["id"], "mode": self.round["mode"],
                             "chats": [c for c in self.chats.values() if c.round_id == self.round["id"]],
                             "deals": self.deals, "posts": self.posts, "private": self.private})
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
                if a.market_due_at is None and self._wants_market(a):
                    a.market_due_at = now
                    self._notify(a.id)
            self.next_market_tick = max(self.next_market_tick + self.cfg.market_interval_s,
                                        now + self.cfg.market_interval_s * 0.5)
        if self.market_open():
            self._tick_v2(now)
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

    # ------------------------------------------------------------------ version 2: news, orders, pulse
    def _tick_v2(self, now: float):
        if not self.v2:
            return
        el = now - self.round["started_at"]
        for n in self.round.get("news", []):
            if not n["tip_done"] and el >= n["tip_at"]:
                n["tip_done"] = True
                self._send_tips(n, now)
            if not n["done"] and el >= n["at"]:
                n["done"] = True
                self._apply_news(n, now)
        for o in self.round.get("orders_plan", []):
            if el >= o["at"] and not any(x["id"] == o["id"] for x in self.orders):
                self._open_order(o, now)
        for o in self.orders:
            if o["status"] == "open" and now >= o["closes_at"]:
                self._close_order(o, now)
        for c in list(self.contracts.values()):
            if c["status"] == "open" and now >= c["expires_at"]:
                self._end_contract(c, "expired", f"not everyone accepted within {int(self.cfg.contract_window_s)} s",
                                   now)
        pn = self.round.get("pulse_next")
        if pn is not None and el >= pn:
            self.round["pulse_next"] = pn + max(1.0, self.cfg.pulse_every_s)
            self._leader_pulse(now)

    def _send_tips(self, n: dict, now: float):
        lead = n["at"] - n["tip_at"]
        sent = []
        for tip in n["tips"]:
            a = self.agents.get(tip["to"])
            if a is None or not a.active:
                continue
            text = tip_text(tip, lead, self.cfg.tip_accuracy)
            a.tips.append({"text": text, "at": now})
            self._notice(a, text)
            self._wake(a, "tip")
            sent.append(a.id)
        self._log("tips", None, news=n["i"], to=sent)

    def _apply_news(self, n: dict, now: float, quiet: bool = False):
        t = n["item"]
        self.mult[t] = min(MULT_MAX, max(MULT_MIN, self.mult[t] * n["factor"]))
        self.news_log.append({"headline": n["headline"], "item": t, "factor": n["factor"],
                              "multiplier": round(self.mult[t], 3), "at": now})
        if quiet:
            return
        text = (f"NEWS: {n['headline']}. The {t} multiplier is now {fmt_factor(round(self.mult[t], 3))}: "
                f"every {t} is worth card value {fmt_factor(round(self.mult[t], 3))}.")
        self._add_post(None, "HOUSE", text, now)
        for a in self.active_agents():
            self._notice(a, text)
            if a.units[t]:
                self._point(a, now)
            self._wake(a, "news")
        self._log("news", None, item=t, factor=n["factor"], multiplier=self.mult[t])

    def _order_cap(self, item: str, premium: float = 1.0) -> int:
        """The most the house pays: the 5-trade ticker average (80 x multiplier before any trades print), times the
        order's premium. Pumping the ticker raises it; that is the point of a wash trade."""
        p = self.prints.get(item) or []
        base = sum(p[-5:]) / len(p[-5:]) if p else BASE_VALUE * self.mult.get(item, 1.0)
        return max(1, int(round(base * premium)))

    def _open_order(self, plan: dict, now: float, cap: int | None = None, quiet: bool = False):
        o = {"id": plan["id"], "buyer": plan["buyer"], "item": plan["item"], "qty": plan["qty"],
             "cap": cap if cap is not None else self._order_cap(plan["item"], plan.get("premium", 1.0)),
             "premium": plan.get("premium", 1.0),
             "opened_at": now, "closes_at": now + self.cfg.house_order_window_s, "asks": {}, "status": "open",
             "filled": 0, "fills": []}
        self.orders.append(o)
        if quiet:
            return o
        if self.store is not None:
            self.store.save_private(self.round["id"], "order", {"kind": "order", "id": o["id"], "cap": o["cap"],
                                                                "seq": self._seq(), "at": now}, now)
        basis = "the 5-trade ticker average" if self.prints.get(o["item"]) else "80 x multiplier, no ticker price yet"
        self._add_post(None, "HOUSE",
                       f"HOUSE ORDER #{o['id']}: {o['buyer']} buys {o['qty']} {o['item']} at up to {o['cap']} coins "
                       f"each ({o['premium']:g} x {basis}). Send a house_ask within "
                       f"{int(self.cfg.house_order_window_s)} s: the lowest asks win, each paid its own ask.", now)
        for a in self.active_agents():
            if a.units[o["item"]] or a.owner is None:
                self._wake(a, "house_order")
        self._log("order_open", None, order=o["id"], item=o["item"], cap=o["cap"])
        return o

    def _close_order(self, o: dict, now: float):
        asks = sorted(o["asks"].items(), key=lambda kv: (kv[1]["price"], kv[1]["seq"]))
        left = o["qty"]
        prices = []
        for aid, ask in asks:
            a = self.agents.get(aid)
            if a is None or not a.active:
                continue
            if left <= 0:
                self._notice(a, f"Your ask on house order #{o['id']} lost: lower asks filled it.")
                continue
            n = min(ask["qty"], left, len(a.units[o["item"]]))
            if n <= 0:
                self._notice(a, f"Your ask on house order #{o['id']} was skipped: you no longer hold {o['item']}.")
                continue
            if a.coins + ask["price"] * n < self.fee():
                self._notice(a, f"Your ask on house order #{o['id']} was skipped: you cannot pay the trade fee.")
                continue
            left -= n
            prices += [ask["price"]] * n
            self._house_fill(o, a, n, ask["price"], now)
        o["status"] = "filled" if left == 0 else ("partly filled" if left < o["qty"] else "expired")
        if prices:
            text = (f"HOUSE ORDER #{o['id']} closed: {o['buyer']} bought {len(prices)} {o['item']} at "
                    f"{', '.join(str(p) for p in prices)} coins.")
        else:
            text = f"HOUSE ORDER #{o['id']} closed: no sale (no asks at or below {o['cap']} coins)."
        self._add_post(None, "HOUSE", text, now)
        self._log("order_close", None, order=o["id"], filled=o["qty"] - left)

    def _house_fill(self, o: dict, a: Agent, n: int, price: int, now: float, record: dict | None = None):
        t = o["item"]
        did = record["id"] if record else self.next_deal_id
        if not record:
            self.next_deal_id += 1
        serials = list(record["a_serials"]) if record else list(a.units[t][:n])
        for s in serials:
            if s in a.units[t]:
                a.units[t].remove(s)
            u = self.units.get(s)
            if u:
                u["owner"] = -1
                u["history"].append([-1, round(now - self.round["started_at"], 1), did])
        fee = record["fee"] if record else self.fee()
        a.coins += price * n - fee
        o["filled"] += n
        o["fills"].append({"seller": a.name, "qty": n, "price": price})
        d = record or {"id": did, "kind": "house_fill", "round_id": self.round["id"], "chat_id": None, "at": now,
                       "seq": self._seq(), "order": o["id"], "a_id": a.id, "b_id": None, "a": a.name, "b": o["buyer"],
                       "item": t, "qty": n, "price": price, "a_gives": {t: n}, "b_gives": {"coins": price * n},
                       "a_serials": list(serials), "b_serials": [], "fee": fee, "seller": a.name, "buyer": o["buyer"],
                       "text": f"{o['buyer']} (house order #{o['id']}) bought {n} {t} from {a.name} for {price} "
                               f"coins each"}
        self.deals.append(d)
        self._point(a, now)
        if record:
            return d
        if self.store is not None:
            self.store.save_deal(d)
            self.store.save_agent(a)
        self._notice(a, f"House order #{o['id']}: you sold {n} {t} to {o['buyer']} for {price} coins each"
                        + (f" (trade fee {fee})" if fee else "") + f". Your Wealth is now {self.W(a)}.")
        self._log("house_fill", a.id, order=o["id"], qty=n, price=price)
        return d

    def _leader_pulse(self, now: float):
        mains = [a for a in self.active_agents() if a.owner is None]
        if not mains:
            return
        ranked = sorted(mains, key=lambda a: (-self.team_score(a.id), a.id))[:3]
        names = [a.name for a in ranked]
        self.pulse = {"top3": names, "at": now}
        listed = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
        lead = {1: "The leader right now is", 2: "The top two right now are"}.get(len(names),
                                                                                    "The top three right now are")
        text = f"LEADER PULSE: {lead} {listed}. (The house shows no numbers.)"
        self._add_post(None, "HOUSE", text, now)
        for a in mains:
            self._wake(a, "pulse")

    # ------------------------------------------------------------------ events
    def _board_slice(self, now: float, n: int) -> list[dict]:
        return [{"id": p["id"], "author": p["author"], "text": p["text"], "ago_s": int(now - p["at"])}
                for p in self.posts[-n:]] if n else []

    def _ledger_slice(self, now: float, n: int) -> list[dict]:
        return [{"text": d["text"], "ago_s": int(now - d["at"])} for d in self.deals[-n:]] if n else []

    def _you(self, a: Agent) -> dict:
        d = {"you": a.name, "holdings": a.holdings(), "card": dict(a.card), "wealth": self.W(a)}
        if self.v2:
            d["values_now"] = self.values_now(a)
            d["multipliers"] = {t: round(m, 3) for t, m in self.mult.items()}
        return d

    def _contract_view(self, c: dict, a: Agent, now: float) -> dict:
        mine = c["parties"][a.id]
        vals = self.values_now(a)
        return {"contract": c["id"], "proposer": self.agents_name(c["proposer"]),
                "parties": {self.agents_name(k): {"give": v["give"], "get": v["get"]} for k, v in c["parties"].items()},
                "you_give": mine["give"], "you_get": mine["get"],
                "wealth_change": value(vals, mine["get"]) - value(vals, mine["give"]) - self.fee(),
                "you_accepted": a.id in c["accepted"],
                "waiting_for": [self.agents_name(k) for k in c["parties"] if k not in c["accepted"]],
                "expires_in_s": int(max(0, c["expires_at"] - now))}

    def _v2_info(self, a: Agent, now: float) -> dict:
        if not self.v2:
            return {}
        tid = self.team_id(a.id)
        main = self.agents.get(tid, a)
        ents = self.entities(tid)
        vals = self.values_now(a)
        team = {"name": main.name, "you_are": "main" if a.owner is None else "shell",
                "entities": [{"name": e.name, "role": "main" if e.owner is None else "shell", "wealth": self.W(e),
                              "holdings": e.holdings()} for e in ents],
                "score_if_bell_now": self.team_score(tid), "set_complete": self.set_complete(tid),
                "shells_left": max(0, self.cfg.shells_per_team - len(self.shells_of.get(tid, [])))
                if not main.is_house else 0}
        orders = [{"order": o["id"], "buyer": o["buyer"], "item": o["item"], "qty": o["qty"], "cap": o["cap"],
                   "closes_in_s": int(max(0, o["closes_at"] - now)),
                   "your_ask": (o["asks"].get(a.id) or {}).get("price"), "you_hold": len(a.units[o["item"]]),
                   "your_value_now": vals.get(o["item"])} for o in self.orders if o["status"] == "open"]
        return {"news": [{"headline": n["headline"], "item": n["item"], "factor": n["factor"],
                          "ago_s": int(now - n["at"])} for n in self.news_log[-6:]],
                "tips": [{"text": t["text"], "ago_s": int(now - t["at"])} for t in main.tips[-4:]],
                "ticker": ticker_view(self.prints), "house_orders": orders,
                "leader_pulse": {"top3": self.pulse["top3"], "ago_s": int(now - self.pulse["at"])}
                if self.pulse else None,
                "team": team,
                "contracts": [self._contract_view(c, a, now) for c in self.contracts.values()
                              if c["status"] == "open" and a.id in c["parties"]],
                "rules": {"trade_fee": self.fee(), "audit_cost": self.cfg.audit_cost, "set_bonus": self.cfg.set_bonus,
                          "shells_per_team": self.cfg.shells_per_team}}

    def _turn_event(self, a: Agent, c: Chat, now: float) -> dict:
        partner = self.agents[c.other(a.id)]
        transcript = []
        for m in c.messages:
            transcript.append({"from": "SERVER" if m.get("system") else self.agents_name(m["author"]),
                               "action": m["action"], "text": m["text"], "offer": m.get("offer")})
        their = c.offers.get(partner.id)
        accept_info = None
        if their:
            fee = self.fee()
            vals = self.values_now(a)
            change = value(vals, their["give"]) - value(vals, their["get"]) - fee
            can_pay = self._can_pay_deal(a, their["get"], their["give"])
            fee_txt = f" (after the {fee}-coin trade fee)" if fee else ""
            accept_info = {"you_give": their["get"], "you_get": their["give"], "wealth_change": change,
                           "you_can_pay": can_pay,
                           "summary": f"{partner.name} offers to give you {fmt_goods(their['give'])} and wants "
                                      f"{fmt_goods(their['get'])} from you. If you accept, your Wealth changes by "
                                      f"{change:+d}{fee_txt}" + ("." if can_pay else
                                                                 f", but you cannot pay: you lack "
                                                                 f"{self._missing_deal(a, their['get'], their['give'])}.")}
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
        ev.update(self._v2_info(a, now))
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
        if a.alerts:
            ev["alert"] = ", ".join(dict.fromkeys(a.alerts))
            a.alerts = []
        ev.update(self._you(a))
        ev.update(self.clock_info(now))
        ev["agents"] = [o.name for o in self.active_agents() if o.id != a.id]
        if self.wealth_public():
            ev["dashboard_wealth"] = {r["name"]: r["wealth"] for r in self.wealth_board()}
        ev["open_chats"] = chats
        ev["board"] = self._board_slice(now, self.cfg.board_in_events)
        ev["ledger"] = self._ledger_slice(now, self.cfg.ledger_in_events)
        ev["posts_left_this_minute"] = self._posts_left(a, now)
        ev["house_notice"] = self.house_notice["text"] if self.house_notice else None
        ev.update(self._v2_info(a, now))
        ev["notices"] = self._take_notices(a)
        return ev

    def _posts_left(self, a: Agent, now: float) -> int:
        while a.post_times and now - a.post_times[0] >= 60:
            a.post_times.popleft()
        return max(0, self.cfg.posts_per_minute - len(a.post_times))

    def _take_notices(self, a: Agent) -> list[str]:
        out, a.notices = a.notices, []
        return out

    def _game_over_event(self, a: Agent) -> dict:
        me = next((s for s in self.final["standings"] if s["id"] == a.id), None) if self.final else None
        notices = []
        for e in self.entities(a.id):
            notices += self._take_notices(e)
        return {"type": "game_over", "event_id": None,
                "rank": me["rank"] if me else None, "of": len(self.final["standings"]) if self.final else 0,
                "wealth": me["wealth"] if me else self.team_score(a.id), "coins": me["coins"] if me else a.coins,
                "winner": self.final["standings"][0]["name"] if self.final and self.final["standings"] else None,
                "standings": [{"rank": s["rank"], "name": s["name"], "wealth": s["wealth"]}
                              for s in (self.final["standings"] if self.final else [])],
                "notices": notices}

    def pop_event(self, aid: int, now: float | None = None) -> dict | None:
        """The next event for a team: any of its entities (main agent and shells), most urgent first."""
        now = self.clock() if now is None else now
        tid = self.team_id(aid)
        main = self.agents.get(tid)
        if main is None:
            return None
        main.last_seen = now
        if self.mode in ("over", "reveal") and main.active and not main.game_over_delivered:
            main.game_over_delivered = True
            return self._game_over_event(main)
        if not main.active or not self.market_open():
            return None
        # Oldest turn first, but prefer turns that can still be answered in time: a backlogged bot
        # should not spend a slow LLM call on a chat that will expire before the answer lands.
        min_left = min(15.0, self.cfg.reply_timeout_s / 3)
        pick = None
        for a in self.entities(tid):
            if not a.active:
                continue
            best, rushed = None, None
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
            turn_key = (0, best[0]) if best else ((1, rushed[0]) if rushed else None)
            turn = (best or rushed or (None, None))[1]
            if a.market_due_at is not None and (turn is None or a.market_due_at <= turn.last_message_at):
                cand = ((0, a.market_due_at), a, None)
            elif turn is not None:
                cand = (turn_key, a, turn)
            else:
                continue
            if pick is None or cand[0] < pick[0]:
                pick = cand
        if pick is None:
            return None
        _key, a, c = pick
        if c is None:
            a.market_due_at = None
            return self._market_event(a, now)
        c.delivered_at = now
        return self._turn_event(a, c, now)

    def wait_event(self, aid: int, now: float | None = None) -> dict:
        now = self.clock() if now is None else now
        a = self.agents.get(self.team_id(aid))
        if a is not None and self.mode in ("over", "reveal") and a.active:
            return self._game_over_event(a)
        ev = {"type": "wait", "event_id": None}
        ev.update(self.clock_info(now))
        if a is not None:
            if not a.active and self.mode == "game":
                ev["info"] = "This game runs without you. Wait for the host to switch back to practice."
            ev["notices"] = []
            for e in self.entities(a.id):
                ev["notices"] += self._take_notices(e)
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

    def _can_pay_deal(self, a: Agent, gives: dict, gets: dict) -> bool:
        """Holds what it gives, and still has coins for the trade fee after the deal."""
        if not self._can_pay(a, gives):
            return False
        return a.coins - gives.get("coins", 0) + gets.get("coins", 0) >= self.fee()

    def _missing_deal(self, a: Agent, gives: dict, gets: dict) -> str:
        if not self._can_pay(a, gives):
            return self._missing(a, gives)
        return f"{self.fee()} coins for the trade fee"

    def _turn_entity(self, tid: int, eid: str) -> Agent | None:
        m = re.fullmatch(r"t-(\d+)-(\d+)", eid)
        c = self.chats.get(int(m.group(1))) if m else None
        if c is None:
            return None
        mine = [x for x in (c.a, c.b) if self.team_id(x) == tid and x in self.agents]
        if not mine:
            return None
        return self.agents[c.whose_turn] if c.whose_turn in mine else self.agents[mine[0]]

    def act(self, aid: int, answer, now: float | None = None) -> dict:
        now = self.clock() if now is None else now
        res = Result()
        main = self.agents.get(self.team_id(aid))
        if main is None:
            res.dropped.append("unknown agent")
            return res.to_dict()
        main.last_seen = now
        if not isinstance(answer, dict):
            res.dropped.append("answer must be a JSON object")
            self._notice(main, "Your last answer was not a JSON object and was ignored.")
            return res.to_dict()
        eid = str(answer.get("event_id") or "")
        a = main
        if not self.market_open():
            res.dropped.append("the market is closed")
        elif not main.active:
            res.dropped.append("you are not in this market round")
        elif eid.startswith("t-"):
            ent = self._turn_entity(main.id, eid)
            if ent is None:
                res.dropped.append("unknown chat in event_id")
            else:
                a = ent
                if self._act_turn(a, answer, eid, res, now):
                    self._maybe_post(a, answer.get("post"), res, now)
                    self._act_extras(a, answer, res, now)
        elif eid.startswith("m-"):
            m = re.fullmatch(r"m-(\d+)-\d+-\d+", eid)
            ent = self.agents.get(int(m.group(1))) if m else None
            if ent is None or self.team_id(ent.id) != main.id or eid not in ent.market_ids:
                res.dropped.append("stale market event: it was already answered or is too old")
            else:
                a = ent
                a.market_ids.remove(eid)
                self._act_market(a, answer, res, now)
                self._maybe_post(a, answer.get("post"), res, now)
                self._act_extras(a, answer, res, now)
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

    def _act_turn(self, a: Agent, answer: dict, eid: str, res: Result, now: float) -> bool:
        """Apply a chat turn. False when the event itself was not valid (stale, unknown, ended)."""
        m = re.fullmatch(r"t-(\d+)-(\d+)", eid)
        c = self.chats.get(int(m.group(1))) if m else None
        if c is None or a.id not in (c.a, c.b):
            res.dropped.append("unknown chat in event_id")
            return False
        if c.status != "open":
            res.dropped.append(f"chat {c.id} has already ended ({c.end_reason})")
            return False
        if c.whose_turn != a.id or len(c.messages) != int(m.group(2)):
            res.dropped.append(f"stale turn for chat {c.id}: it is not your turn or this turn was already answered")
            return False
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
                    return True
                self._record(c, None, f"Deal failed: {why}. The chat goes on.", "system", None, now, system=True)
                for side in (a, partner):
                    o = c.offers.get(side.id)
                    if o and not self._can_pay(side, o["give"]):
                        del c.offers[side.id]
                self._notice(partner, f"Deal failed in chat with {a.name}: {why}. The chat goes on.")
                res.dropped.append(f"deal failed: {why}")
                self._pass_turn(c, a, partner, now)
                return True
        if action == "end":
            self._record(c, a.id, text or "I'm out.", "end", None, now)
            c.ended_by = a.id
            self._end_chat(c, f"{a.name} walked away", now)
            res.applied.append("chat ended")
            return True
        if not text:
            text = "(offer)" if action == "offer" else "..."
        self._record(c, a.id, text, action, offer, now)
        if action == "talk":
            res.applied.append("message sent")
        self._pass_turn(c, a, partner, now)
        return True

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

    # ------------------------------------------------------------------ version 2 answer parts
    def _act_extras(self, a: Agent, answer: dict, res: Result, now: float):
        given = [k for k in EXTRA_KEYS if _given(answer.get(k))]
        if not given:
            return
        if not self.v2:
            for k in given:
                res.dropped.append(f"'{k}' is not part of this game (classic rules)")
            return
        if "create_shell" in given:
            self._act_create_shell(a, answer["create_shell"], res)
        if "transfer" in given:
            self._act_transfer(a, answer["transfer"], res, now)
        if "house_ask" in given:
            self._act_house_ask(a, answer["house_ask"], res, now)
        if "contract_reply" in given:
            self._act_contract_reply(a, answer["contract_reply"], res, now)
        if "contract" in given:
            self._act_contract(a, answer["contract"], res, now)
        if "audit" in given:
            self._act_audit(a, answer["audit"], res, now)

    def _resolve_exact(self, raw) -> Agent | None:
        if raw is None or isinstance(raw, (dict, list)):
            return None
        s = re.sub(r"\s+", " ", str(raw)).strip().lstrip("@").lower()
        aid = self.by_name.get(s)
        if aid is not None and self.agents[aid].active:
            return self.agents[aid]
        return self.resolve_name(raw)

    def _act_create_shell(self, a: Agent, raw, res: Result):
        names = raw if isinstance(raw, list) else [raw]
        for n in names[:2]:
            if isinstance(n, dict):
                n = n.get("name")
            try:
                s = self.create_shell(a, n)
            except GameError as e:
                res.dropped.append(f"create_shell dropped: {e}")
                continue
            res.applied.append(f"shell company '{s.name}' created for your team (it starts empty; send it goods "
                               f"with a transfer)")

    def _act_transfer(self, a: Agent, raw, res: Result, now: float):
        for x in (raw if isinstance(raw, list) else [raw])[:5]:
            if not isinstance(x, dict):
                res.dropped.append('transfer dropped: use {"to": "<your shell or main>", "give": {"coins": 100}}')
                continue
            to = self._resolve_exact(x.get("to", x.get("agent", x.get("name"))))
            if to is None or not to.active or self.team_id(to.id) != self.team_id(a.id):
                res.dropped.append(f"transfer dropped: '{x.get('to')}' is not one of your team's companies")
                continue
            if to.id == a.id:
                res.dropped.append("transfer dropped: a company cannot transfer to itself")
                continue
            goods, errs = norm_goods(x.get("give", x.get("goods", x.get("items"))), "give")
            if not goods:
                res.dropped.append("transfer dropped: say what to give, e.g. {\"give\": {\"coins\": 100}}"
                                   + (" (" + "; ".join(errs) + ")" if errs else ""))
                continue
            if not self._can_pay(a, goods):
                res.dropped.append(f"transfer dropped: {a.name} does not hold {self._missing(a, goods)}")
                continue
            moved = self._move(a, to, goods, now, None)
            rec = {"kind": "transfer", "seq": self._seq(), "at": now, "from_id": a.id, "to_id": to.id,
                   "from": a.name, "to": to.name, "goods": goods, "serials": moved}
            self._save_private(rec, now)
            self._point(a, now)
            self._point(to, now)
            self._notice(a, f"Private transfer done: {a.name} gave {fmt_goods(goods)} to {to.name} "
                            f"(not on the ledger).")
            res.applied.append(f"private transfer: {fmt_goods(goods)} to {to.name}")
            self._wake(to, "transfer")

    def _save_private(self, rec: dict, now: float):
        self.private.append(rec)
        if self.store is not None:
            self.store.save_private(self.round["id"], rec["kind"], rec, now)
            for k in ("from_id", "to_id", "aid"):
                if rec.get(k) in self.agents:
                    self.store.save_agent(self.agents[rec[k]])

    def _act_house_ask(self, a: Agent, raw, res: Result, now: float):
        for x in (raw if isinstance(raw, list) else [raw])[:5]:
            if not isinstance(x, dict):
                res.dropped.append('house_ask dropped: use {"order": 3, "price": 70, "qty": 1}')
                continue
            oid = _int(x.get("order", x.get("order_id", x.get("id"))))
            o = next((o for o in self.orders if o["id"] == oid), None)
            if o is None or o["status"] != "open":
                res.dropped.append(f"house_ask dropped: house order #{x.get('order')} is not open")
                continue
            price = _int(x.get("price", x.get("ask")))
            qty = _int(x.get("qty", x.get("quantity", 1))) or 1
            if price is None or price < 1:
                res.dropped.append("house_ask dropped: price must be a whole number of coins")
                continue
            if price > o["cap"]:
                res.dropped.append(f"house_ask dropped: {price} is above the cap of {o['cap']} for order #{o['id']}")
                continue
            if not 1 <= qty <= o["qty"]:
                res.dropped.append(f"house_ask dropped: qty must be 1 to {o['qty']}")
                continue
            have = len(a.units[o["item"]])
            if have < qty:
                res.dropped.append(f"house_ask dropped: {a.name} holds {have} {o['item']}, not {qty}")
                continue
            o["asks"][a.id] = {"price": price, "qty": qty, "at": now, "seq": self._seq()}
            res.applied.append(f"house ask placed: order #{o['id']}, {qty} {o['item']} at {price} coins each; the "
                               f"lowest asks win when it closes in {int(max(0, o['closes_at'] - now))} s")

    def _act_audit(self, a: Agent, raw, res: Result, now: float):
        if isinstance(raw, dict):
            names = [raw.get("a", raw.get("x", raw.get("first"))), raw.get("b", raw.get("y", raw.get("second")))]
        elif isinstance(raw, str):
            names = [p for p in re.split(r"\s*(?:,|\band\b|&)\s*", raw) if p.strip()]
        else:
            names = list(raw) if isinstance(raw, (list, tuple)) else []
        if len(names) != 2:
            res.dropped.append('audit dropped: name two agents, e.g. "audit": ["Bean There", "Crema Cartel"]')
            return
        x, y = self._resolve_exact(names[0]), self._resolve_exact(names[1])
        if x is None or y is None:
            res.dropped.append(f"audit dropped: no agent named '{names[0] if x is None else names[1]}' in the market")
            return
        cost = int(self.cfg.audit_cost)
        if a.coins < cost:
            res.dropped.append(f"audit dropped: an audit costs {cost} coins and {a.name} has {a.coins}")
            return
        a.coins -= cost
        same = self.team_id(x.id) == self.team_id(y.id)
        rec = {"kind": "audit", "seq": self._seq(), "at": now, "aid": a.id, "by": a.name, "x_id": x.id, "y_id": y.id,
               "x": x.name, "y": y.name, "same": same, "cost": cost}
        self._save_private(rec, now)
        self._point(a, now)
        text = (f"AUDIT (private, always true): {x.name} and {y.name} {'ARE' if same else 'are NOT'} owned by the "
                f"same team. It cost {cost} coins.")
        self._notice(a, text)
        res.applied.append(text)

    def _act_contract(self, a: Agent, raw, res: Result, now: float):
        if not isinstance(raw, dict):
            res.dropped.append('contract dropped: use {"parties": {"<name>": {"give": {...}, "get": {...}}, ...}}')
            return
        pr = raw.get("parties", raw)
        if isinstance(pr, list):
            conv = {}
            for p in pr:
                if isinstance(p, dict):
                    conv[str(p.get("name", p.get("agent", p.get("party", ""))))] = p
            pr = conv
        if not isinstance(pr, dict) or not 2 <= len(pr) <= 4:
            res.dropped.append("contract dropped: a contract has 2 to 4 parties, you included")
            return
        parties = {}
        for name, terms in pr.items():
            ag = self._resolve_exact(name)
            if ag is None:
                res.dropped.append(f"contract dropped: no agent named '{name}' in the market")
                return
            if ag.id in parties:
                res.dropped.append(f"contract dropped: {ag.name} is listed twice")
                return
            terms = terms if isinstance(terms, dict) else {}
            give, e1 = norm_goods(terms.get("give"), "give")
            get, e2 = norm_goods(terms.get("get"), "get")
            if e1 or e2:
                res.dropped.append(f"contract dropped: terms for {ag.name}: " + "; ".join(e1 + e2))
                return
            parties[ag.id] = {"give": give, "get": get}
        if a.id not in parties:
            res.dropped.append("contract dropped: you must be one of the parties")
            return
        errs = contract_errors({self.agents_name(k): v for k, v in parties.items()})
        if errs:
            res.dropped.append("contract dropped: " + "; ".join(errs))
            return
        if not self._can_pay(a, parties[a.id]["give"]):
            res.dropped.append(f"contract dropped: you do not hold {self._missing(a, parties[a.id]['give'])}")
            return
        if sum(1 for c in self.contracts.values() if c["status"] == "open" and c["proposer"] == a.id) >= 3:
            res.dropped.append("contract dropped: you already have 3 open contract proposals")
            return
        c = {"id": self.next_contract_id, "proposer": a.id, "parties": parties, "accepted": {a.id},
             "status": "open", "created_at": now, "expires_at": now + self.cfg.contract_window_s}
        self.next_contract_id += 1
        self.contracts[c["id"]] = c
        others = [self.agents[k] for k in parties if k != a.id]
        for o in others:
            self._notice(o, f"Contract #{c['id']} from {a.name}: {self._contract_text(c)}. You give "
                            f"{fmt_goods(parties[o.id]['give'])}, you get {fmt_goods(parties[o.id]['get'])}. Answer "
                            f"with contract_reply within {int(self.cfg.contract_window_s)} s.")
            self._wake(o, "contract", force=True)
        res.applied.append(f"contract #{c['id']} proposed to {', '.join(o.name for o in others)}; it executes when "
                           f"everyone accepts within {int(self.cfg.contract_window_s)} s")
        self._log("contract", a.id, contract=c["id"], parties=list(parties))

    def _contract_text(self, c: dict) -> str:
        return "; ".join(f"{self.agents_name(k)} gives {fmt_goods(v['give'])}" for k, v in c["parties"].items()
                         if v["give"])

    def _act_contract_reply(self, a: Agent, raw, res: Result, now: float):
        for x in (raw if isinstance(raw, list) else [raw])[:5]:
            if not isinstance(x, dict):
                res.dropped.append('contract_reply dropped: use {"contract": 7, "accept": true}')
                continue
            cid = _int(x.get("contract", x.get("id", x.get("contract_id"))))
            c = self.contracts.get(cid)
            if c is None or c["status"] != "open":
                res.dropped.append(f"contract_reply dropped: contract #{x.get('contract', x.get('id'))} is not open")
                continue
            if a.id not in c["parties"]:
                res.dropped.append(f"contract_reply dropped: {a.name} is not a party to contract #{cid}")
                continue
            acc = x.get("accept", x.get("answer"))
            accept = acc is True or (isinstance(acc, str) and acc.strip().lower() in ("yes", "accept", "true", "ok"))
            if not accept:
                self._end_contract(c, "declined", f"{a.name} declined", now)
                res.applied.append(f"declined contract #{cid}")
                continue
            c["accepted"].add(a.id)
            res.applied.append(f"accepted contract #{cid}")
            if len(c["accepted"]) == len(c["parties"]):
                self._execute_contract(c, now, res)

    def _end_contract(self, c: dict, status: str, why: str, now: float):
        c["status"] = status
        for k in c["parties"]:
            if k in self.agents:
                self._notice(self.agents[k], f"Contract #{c['id']} {status}: {why}.")
                self._notify(k)

    def _execute_contract(self, c: dict, now: float, res: Result | None = None, record: dict | None = None):
        fee = record["fee"] if record else self.fee()
        if not record:
            for pid, t in c["parties"].items():
                p = self.agents.get(pid)
                if p is None or not p.active:
                    why = f"{self.agents_name(pid)} left the market"
                elif not self._can_pay(p, t["give"]) or p.coins - t["give"].get("coins", 0) + \
                        t["get"].get("coins", 0) < fee:
                    why = f"{p.name} cannot pay ({self._missing_deal(p, t['give'], t['get'])})"
                else:
                    continue
                self._end_contract(c, "failed", why, now)
                if res is not None:
                    res.dropped.append(f"contract #{c['id']} failed: {why}")
                return False
        did = record["id"] if record else self.next_deal_id
        if not record:
            self.next_deal_id += 1
        t_rel = round(now - self.round["started_at"], 1)
        pool: dict = {t: [] for t in ITEMS}
        legs = record["legs"] if record else []
        plan = [(l["aid"], {"give": l["give"], "get": l["get"]}, l) for l in legs] if record else \
            [(pid, t, None) for pid, t in c["parties"].items()]
        out_serials = {}
        for pid, t, leg in plan:
            p = self.agents[pid]
            p.coins -= t["give"].get("coins", 0)
            given = []
            for k, q in t["give"].items():
                if k == "coins":
                    continue
                take = leg["give_serials"] if leg else None
                for i in range(q):
                    s = next(x for x in take if self.units.get(x, {}).get("type") == k and x in p.units[k]) \
                        if take else p.units[k][0]
                    if take:
                        take = [x for x in take if x != s]
                    p.units[k].remove(s)
                    pool[k].append(s)
                    given.append(s)
            out_serials[pid] = given
        new_legs = []
        for pid, t, leg in plan:
            p = self.agents[pid]
            p.coins += t["get"].get("coins", 0) - fee
            got = []
            for k, q in t["get"].items():
                if k == "coins":
                    continue
                for _ in range(q):
                    s = next(x for x in leg["get_serials"] if x in pool[k]) if leg else pool[k][0]
                    pool[k].remove(s)
                    p.units[k].append(s)
                    u = self.units.get(s)
                    if u:
                        u["owner"] = p.id
                        u["history"].append([p.id, t_rel, did])
                    got.append(s)
                p.units[k].sort()
            new_legs.append({"aid": pid, "name": p.name, "give": t["give"], "get": t["get"],
                             "give_serials": out_serials[pid], "get_serials": got})
            self._point(p, now)
        text = f"Contract #{c['id']}: " + self._contract_text(c)
        d = record or {"id": did, "kind": "contract", "contract": c["id"], "round_id": self.round["id"],
                       "chat_id": None, "at": now, "seq": self._seq(), "a_id": c["proposer"], "b_id": None,
                       "a": self.agents_name(c["proposer"]), "b": None, "legs": new_legs, "fee": fee, "text": text,
                       "seller": None, "buyer": None}
        self.deals.append(d)
        c["status"] = "done"
        if record:
            return True
        if self.store is not None:
            self.store.save_deal(d)
            for pid in c["parties"]:
                self.store.save_agent(self.agents[pid])
        for pid, t in c["parties"].items():
            p = self.agents[pid]
            self._notice(p, f"Contract #{c['id']} executed: you gave {fmt_goods(t['give'])}, you got "
                            f"{fmt_goods(t['get'])}" + (f" (trade fee {fee})" if fee else "")
                            + f". Your Wealth is now {self.W(p)}.")
            self._notify(pid)
        if res is not None:
            res.applied.append(f"contract #{c['id']} executed")
        self._log("contract_done", None, contract=c["id"], deal=did)
        return True

    # ------------------------------------------------------------------ deals and chat endings
    def _move(self, frm: Agent, to: Agent, goods: dict, now: float, deal_id: int | None) -> list[str]:
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
        fee = self.fee()
        for side, gives, gets in ((owner, offer["give"], offer["get"]), (acceptor, offer["get"], offer["give"])):
            if fee and not self._can_pay_deal(side, gives, gets):
                return False, f"{side.name} cannot pay the {fee}-coin trade fee"
        did = self.next_deal_id
        self.next_deal_id += 1
        a_serials = self._move(owner, acceptor, offer["give"], now, did)
        b_serials = self._move(acceptor, owner, offer["get"], now, did)
        owner.coins -= fee
        acceptor.coins -= fee
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
        d = {"id": did, "kind": "deal", "round_id": self.round["id"], "chat_id": c.id, "at": now, "a_id": owner.id,
             "b_id": acceptor.id, "a": owner.name, "b": acceptor.name, "a_gives": g, "b_gives": r,
             "a_serials": a_serials, "b_serials": b_serials, "seller": seller.name, "buyer": buyer.name,
             "text": text, "fee": fee, "seq": self._seq()}
        if self.v2:
            tp = ticker_print(g, r)
            if tp:
                self.prints[tp[0]].append(tp[1])
                d["print"] = list(tp)
        self.deals.append(d)
        for side in (owner, acceptor):
            self._point(side, now)
        if self.store is not None:
            self.store.save_deal(d)
            self.store.save_agent(owner)
            self.store.save_agent(acceptor)
        self._log("deal", None, **{k: v for k, v in d.items() if k not in ("text", "kind")})
        fee_txt = f" Trade fee: {fee} coins." if fee else ""
        self._notice(owner, f"Deal with {acceptor.name}: you gave {fmt_goods(g)}, you got {fmt_goods(r)}.{fee_txt} "
                            f"Your Wealth is now {self.W(owner)}.")
        self._notice(acceptor, f"Deal with {owner.name}: you gave {fmt_goods(r)}, you got {fmt_goods(g)}.{fee_txt} "
                               f"Your Wealth is now {self.W(acceptor)}.")
        res.applied.append(f"deal: you gave {fmt_goods(r)}, you got {fmt_goods(g)}")
        res.deal = {"id": did, "text": text, "your_wealth": self.W(acceptor)}
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
        if self.v2:
            for o in self.orders:  # asks already sent still count
                if o["status"] == "open":
                    self._close_order(o, now)
        for c in list(self.contracts.values()):
            if c["status"] == "open":
                self._end_contract(c, "expired", "the bell rang", now)
        for cid in list(self.open_chat_ids):
            self._end_chat(self.chats[cid], "the bell rang", now)
        self.round["ends_at"] = min(self.round["ends_at"], now)
        standings = []
        for a in [x for x in self.active_agents() if x.owner is None]:
            ents = self.entities(a.id)
            bonus = self.set_bonus(a.id)
            score = sum(self.W(e) for e in ents) + bonus
            standings.append({"id": a.id, "name": a.name, "house": a.is_house, "wealth": score,
                              "coins": sum(e.coins for e in ents),
                              "items": {t: sum(len(e.units[t]) for e in ents) for t in ITEMS},
                              "card": dict(a.card), "values_now": self.values_now(a), "set_bonus": bonus,
                              "gain": score - a.start_wealth,
                              "shells": [{"name": e.name, "wealth": self.W(e), "coins": e.coins, "items": e.items()}
                                         for e in ents[1:]],
                              "flip": self.rng.random()})
        standings.sort(key=lambda s: (-s["wealth"], -s["coins"], s["flip"]))
        for i, s in enumerate(standings):
            s["rank"] = i + 1
            s["coin_flip"] = any(o is not s and o["wealth"] == s["wealth"] and o["coins"] == s["coins"]
                                 for o in standings)
            del s["flip"]
        self.final = {"round": self.round["id"], "ended_at": now, "standings": standings,
                      "fun_facts": self.fun_facts(), "deals": len(self.deals), "posts": len(self.posts)}
        if self.v2:
            self.final["v2"] = self.v2_summary()
        self.mode = "over"
        if self.store is not None:
            self.store.end_round(self.round["id"], now, self.final)
            self.store.update_round_end(self.round["id"], self.round["ends_at"])
        self._log("bell", None, round=self.round["id"])
        for a in self.active_agents():
            a.game_over_delivered = False
            self._notify(a.id)

    def v2_summary(self) -> dict:
        """For the reveal: news, final multipliers, who owned which shell, and every wash trade."""
        network = []
        for tid, sids in self.shells_of.items():
            if tid in self.agents and sids:
                network.append({"team": self.agents_name(tid), "shells": [self.agents_name(s) for s in sids]})
        wash = []
        for d in self.deals:
            ids = [l[0] for l in self._legs(d) if l[0] is not None]
            teams = [self.team_id(x) for x in ids]
            if len(teams) != len(set(teams)):
                tid = next(t for t in teams if teams.count(t) > 1)
                wash.append({"team": self.agents_name(tid), "text": d["text"]})
        transfers = [{"team": self.agents_name(self.team_id(r["from_id"])), "from": r["from"], "to": r["to"],
                      "goods": fmt_goods(r["goods"])} for r in self.private if r["kind"] == "transfer"]
        audits = [{"by": r["by"], "x": r["x"], "y": r["y"], "same": r["same"]} for r in self.private
                  if r["kind"] == "audit"]
        return {"multipliers": {t: round(m, 3) for t, m in self.mult.items()},
                "news": [{"headline": n["headline"], "multiplier": n["multiplier"]} for n in self.news_log],
                "shell_network": network, "wash_trades": wash, "transfers": transfers, "audits": audits,
                "house_fills": sum(1 for d in self.deals if d.get("kind") == "house_fill")}

    def _legs(self, d: dict) -> list[tuple]:
        """(agent id, gives, gets) for each side of a ledger entry."""
        kind = d.get("kind") or "deal"
        if kind == "deal":
            return [(d["a_id"], d["a_gives"], d["b_gives"]), (d["b_id"], d["b_gives"], d["a_gives"])]
        if kind == "house_fill":
            return [(d["a_id"], d["a_gives"], d["b_gives"])]
        if kind == "contract":
            return [(l["aid"], l["give"], l["get"]) for l in d["legs"]]
        return []

    def fun_facts(self) -> dict:
        facts: dict = {}
        best = None
        for d in self.deals:
            teams = [self.team_id(x[0]) for x in self._legs(d) if x[0] is not None]
            if len(teams) != len(set(teams)):
                continue  # a wash trade moves nothing for the team; the reveal lists those separately
            for aid, gives, gets in self._legs(d):
                a = self.agents.get(aid)
                if not a:
                    continue
                vals = self.values_now(a)
                gain = value(vals, gets) - value(vals, gives) - d.get("fee", 0)
                if best is None or gain > best["gain"]:
                    best = {"agent": self._label(a), "gain": gain, "deal": d["text"]}
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
            facts["most_chats"] = {"agent": self._label(self.agents.get(aid)), "chats": counts[aid]}
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
                walk = {"score": score, "agent": self._label(self.agents.get(c.ended_by)),
                        "partner": self._label(self.agents.get(c.other(c.ended_by))), "messages": c.n_msgs,
                        "last_words": last}
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
            vals = self.values_now(a)
            gain = 0
            for d in self.deals:
                if p["at"] <= d["at"] <= p["at"] + 180:
                    for lid, gives, gets in self._legs(d):
                        if lid == aid:
                            gain += value(vals, gets) - value(vals, gives)
            if gain > 0 and (best is None or gain > best["gain"]):
                best = {"agent": self._label(a), "post": p["text"], "gain": gain}
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
        rows = [{"name": a.name, "wealth": self.W(a), "gain": self.W(a) - a.start_wealth, "house": a.is_house}
                for a in self.active_agents()]
        rows.sort(key=lambda r: -r["wealth"])
        return rows

    def dashboard(self, full: bool = False) -> dict:
        """The public dashboard, for every human and every agent: deals, board, ticker, news, house orders, and
        who is chatting with whom. Never the words of a chat (agents read only their own chats). Under version 2
        each agent's Wealth is on the host's screen only (full=True) until the bell: public Wealth numbers would
        give away the private transfers between a team and its shells."""
        now = self.clock()
        started = self.round["started_at"] if self.round else now
        show = full or self.wealth_public() or self.mode in ("over", "reveal")
        agents = []
        deal_count: dict = {}
        for d in self.deals:
            for aid, _g, _r in self._legs(d):
                deal_count[aid] = deal_count.get(aid, 0) + 1
        for a in sorted(self.active_agents(), key=lambda x: x.id):
            row = {"id": a.id, "name": a.name, "house": a.is_house, "open_chats": len(a.open_chats),
                   "deals": deal_count.get(a.id, 0)}
            if show:
                row.update({"wealth": self.W(a), "gain": self.W(a) - a.start_wealth, "series": a.series})
            agents.append(row)
        chats = sorted(self._round_chats(), key=lambda c: (c.status != "open", -c.last_message_at))
        st = {"mode": self.mode, "now": round(now - started, 1), "clock": self.clock_info(now),
              "registration_open": self.registration_open, "house_notice": self.house_notice,
              "length_s": (self.round["ends_at"] - started) if self.round and self.round.get("ends_at")
              else self.cfg.practice_reset_minutes * 60,
              "agents": agents, "wealth_hidden": not show, "v2": self.v2,
              "links": [[self.chats[cid].a, self.chats[cid].b, self.chats[cid].n_msgs] for cid in self.open_chat_ids],
              "chats": [{"id": c.id, "a": self.agents_name(c.a), "b": self.agents_name(c.b), "status": c.status,
                         "n": c.n_msgs, "end_reason": c.end_reason, "ago_s": int(now - c.last_message_at)}
                        for c in chats[:300]],
              "board": self.board(limit=60)["posts"][::-1],
              "ledger": [{"text": d["text"], "ago_s": int(now - d["at"])} for d in self.deals[-30:]][::-1],
              "stats": {"deals": len(self.deals), "posts": len(self.posts), "open_chats": len(self.open_chat_ids),
                        "chats_total": len(chats)},
              "practice": list(self.practice_history)[-6:]}
        if self.v2:
            st["ticker"] = ticker_view(self.prints)
            st["multipliers"] = {t: round(m, 3) for t, m in self.mult.items()}
            st["news"] = [{"headline": n["headline"], "ago_s": int(now - n["at"])} for n in self.news_log][::-1]
            st["house_orders"] = [{"id": o["id"], "buyer": o["buyer"], "item": o["item"], "qty": o["qty"],
                                   "cap": o["cap"], "status": o["status"], "filled": o["filled"],
                                   "closes_in_s": int(max(0, o["closes_at"] - now)), "asks": len(o["asks"])}
                                  for o in self.orders][::-1][:8]
            st["leader_pulse"] = self.pulse["top3"] if self.pulse else None
        if self.mode in ("over", "reveal") and self.final:
            st["final"] = {"standings": [{k: s[k] for k in ("rank", "name", "wealth", "house")}
                                         for s in self.final["standings"]]}
            if self.mode == "reveal":
                st["final"] = self.final
        return st

    def watch_chats(self, agent: str | None = None) -> list[dict]:
        """For human spectators: every chat of the round with its pinned offers and last message."""
        now = self.clock()
        out, ended = [], 0
        for c in sorted(self._round_chats(), key=lambda c: (c.status != "open", -c.last_message_at)):
            names = (self.agents_name(c.a), self.agents_name(c.b))
            if agent and agent not in names:
                continue
            if c.status != "open":  # every screen polls this every 2 s: all live chats, only the latest finished ones
                ended += 1
                if ended > WATCH_ENDED_MAX:
                    break
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
        """A team's own record (testing): every chat, deal and post of its main agent and shells."""
        now = self.clock()
        main = self.agents.get(self.team_id(a.id), a)
        ents = self.entities(main.id)
        ids = {e.id for e in ents}
        rounds = list(self.archive)
        if self.round:
            rounds.append({"round": self.round["id"], "mode": self.round["mode"],
                           "chats": [c for c in self.chats.values() if c.round_id == self.round["id"]],
                           "deals": self.deals, "posts": self.posts, "private": self.private})
        seen = set()
        chats, deals, posts, private = [], [], [], []
        for r in rounds:
            for c in r["chats"]:
                if (c.a in ids or c.b in ids) and c.id not in seen:
                    seen.add(c.id)
                    v = self._chat_view(c, n_msgs=200)
                    v["round"] = r["round"]
                    chats.append(v)
            deals += [dict(round=r["round"], text=d["text"], at_s=round(d["at"] - (self.round or {}).get(
                "started_at", 0), 1)) for d in r["deals"] if any(l[0] in ids for l in self._legs(d))]
            posts += [{"round": r["round"], "text": p["text"]} for p in r["posts"] if p.get("author_id") in ids]
            for x in r.get("private", []):
                if x.get("from_id") in ids or x.get("aid") in ids:
                    private.append({"round": r["round"], **{k: v for k, v in x.items()
                                                            if k in ("kind", "from", "to", "goods", "by", "x", "y",
                                                                     "same", "cost")}})
        gains = [{"round": h["round"], "gain": h["gains"].get(main.name)} for h in self.practice_history
                 if main.name in h["gains"]]
        out = {"team": main.name, "mode": self.mode, "clock": self.clock_info(now), "active": main.active,
               "wealth": self.W(main) if main.active else None,
               "team_score": self.team_score(main.id) if main.active else None,
               "gain_this_round": self.team_score(main.id) - main.start_wealth if main.active else None,
               "holdings": main.holdings() if main.active else None,
               "card": main.card if main.active else None, "gain_per_reset": gains,
               "entities": [{"name": e.name, "role": "main" if e.owner is None else "shell",
                             "wealth": self.W(e) if e.active else None,
                             "holdings": e.holdings() if e.active else None} for e in ents],
               "chats": chats[-25:], "deals": deals[-50:], "posts": posts[-50:], "private": private[-50:],
               "notices": sorted((n for e in ents for n in e.recent_notices), key=lambda n: n["at"])[-30:]}
        return out

    def host_state(self) -> dict:
        now = self.clock()
        ordered = sorted(self.agents.values(), key=lambda x: (x.is_house, self.team_id(x.id), x.owner is not None,
                                                              x.id))
        return {"mode": self.mode, "clock": self.clock_info(now), "registration_open": self.registration_open,
                "config": self.cfg.to_dict(), "house_notice": self.house_notice,
                "teams": [{"id": a.id, "name": a.name, "key": a.key, "house": a.is_house, "active": a.active,
                           "members": a.members, "shell_of": self.agents_name(a.owner) if a.owner else None,
                           "wealth": self.W(a) if a.active else None,
                           "team_score": self.team_score(a.id) if a.active and a.owner is None else None,
                           "last_seen_s": int(now - a.last_seen) if a.last_seen else None,
                           "open_chats": len(a.open_chats), "requests": a.requests}
                          for a in ordered],
                "stats": {"deals": len(self.deals), "posts": len(self.posts), "open_chats": len(self.open_chat_ids),
                          "teams": len(self.teams()), "shells": sum(len(v) for v in self.shells_of.values())},
                "final": self.final}

    def export(self) -> dict:
        """Everything about the current (or just finished) round, for the debrief (host only)."""
        rid = self.round["id"] if self.round else None
        chats = [c for c in self.chats.values() if c.round_id == rid]
        out = {"round": rid, "mode": self.mode,
               "agents": [{"name": a.name, "house": a.is_house, "members": a.members,
                           "shell_of": self.agents_name(a.owner) if a.owner else None, "card": a.card,
                           "values_now": self.values_now(a), "start_coins": a.start_coins,
                           "start_wealth": a.start_wealth, "coins": a.coins, "items": a.items(),
                           "wealth": self.W(a)} for a in self.active_agents()],
               "chats": [self._chat_view(c, n_msgs=10_000) for c in chats],
               "deals": [{k: v for k, v in d.items()} for d in self.deals],
               "posts": [{"author": p["author"], "text": p["text"], "at": p["at"]} for p in self.posts],
               "units": self.units, "final": self.final, "practice_history": list(self.practice_history)}
        if self.round is not None:
            out["v2"] = {"rules": self.v2, "multipliers": self.mult, "news_plan": [
                dict(n, tips=[dict(t, to=self.agents_name(t["to"])) for t in n["tips"]])
                for n in self.round.get("news", [])],
                "ticker": self.prints,
                "house_orders": [dict(o, asks={self.agents_name(k): v for k, v in o["asks"].items()})
                                 for o in self.orders],
                "transfers": [r for r in self.private if r["kind"] == "transfer"],
                "audits": [r for r in self.private if r["kind"] == "audit"],
                "contracts": [{"id": c["id"], "proposer": self.agents_name(c["proposer"]), "status": c["status"],
                               "parties": {self.agents_name(k): v for k, v in c["parties"].items()}}
                              for c in self.contracts.values()],
                "shell_network": self.v2_summary()["shell_network"]}
        return out

    def transcripts_text(self) -> str:
        ex = self.export()
        lines = [f"# The Espresso Exchange: transcripts, round {ex['round']} ({ex['mode']})", ""]
        if ex["final"]:
            lines.append("## Final standings")
            for s in ex["final"]["standings"]:
                card = ", ".join(f"{k} {v}" for k, v in sorted(s["card"].items(), key=lambda kv: -kv[1]))
                extra = ""
                if s.get("shells"):
                    extra += "; shells: " + ", ".join(x["name"] for x in s["shells"])
                if s.get("set_bonus"):
                    extra += f"; set bonus {s['set_bonus']}"
                lines.append(f"{s['rank']}. {s['name']}: Wealth {s['wealth']} ({s['coins']} coins; card: {card}"
                             f"{extra})")
            lines.append("")
        lines.append("## Ledger")
        lines += [f"- {d['text']}" for d in ex["deals"]] or ["- no deals"]
        lines += ["", "## Board"]
        lines += [f"- {p['author']}: {p['text']}" for p in ex["posts"]] or ["- no posts"]
        if ex.get("v2") and (ex["v2"]["transfers"] or ex["v2"]["audits"]):
            lines += ["", "## Private moves (host only)"]
            lines += [f"- transfer: {r['from']} gave {fmt_goods(r['goods'])} to {r['to']}" for r in ex["v2"]["transfers"]]
            lines += [f"- audit by {r['by']}: {r['x']} and {r['y']} same team: {r['same']}" for r in ex["v2"]["audits"]]
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
        """Reload teams (with members and shells); resume a game round that was running; otherwise open a
        practice round."""
        if self.store is None:
            return False
        for row in self.store.load_agents():
            name = row["name"]
            if row["is_house"] and name in LEGACY_HOUSE_NAMES:
                name = LEGACY_HOUSE_NAMES[name]
                self.store.db.execute("UPDATE agents SET name=? WHERE id=?", (name, row["id"]))
            a = Agent(id=row["id"], name=name, key=row["key"] or "", is_house=bool(row["is_house"]),
                      created_at=row["created_at"] or 0.0, members=row.get("members") or [], owner=row.get("owner"))
            self.agents[a.id] = a
            if a.key:
                self.by_key[a.key] = a.id
            self.by_name[a.name.lower()] = a.id
            for m in a.members:
                try:
                    self.member_keys[clean_person(m)[1]] = a.id
                except ValueError:
                    pass
        for a in list(self.agents.values()):
            if a.owner is not None:
                if a.owner in self.agents:
                    self.shells_of.setdefault(a.owner, []).append(a.id)
                else:  # its team is gone
                    del self.agents[a.id]
                    self.by_name.pop(a.name.lower(), None)
        last = self.store.last_round()
        now = self.clock()
        if last and last["mode"] == "game" and last["ended_at"] is None and (last["ends_at"] or 0) > now:
            self._restore_game(last, now)
            return True
        return False

    def _restore_game(self, last: dict, now: float):
        setup = json.loads(last["setup"])
        started = last["started_at"]
        self.round_seq = last["id"]
        self.mode = "game"
        self.registration_open = False
        self.round = {"id": last["id"], "mode": "game", "started_at": started, "ends_at": last["ends_at"],
                      "perm": setup["perm"], "cols": setup["cols"], "next_serial": setup["next_serial"],
                      "latin_k": len(setup["agents"]), "last_call_done":
                          now >= started + self.cfg.last_call_minutes * 60, "reset_at": None,
                      "news": setup.get("news", []), "orders_plan": setup.get("orders", []), "pulse_next": None}
        self.units = {}
        self.mult = {t: 1.0 for t in ITEMS}
        self.prints = {t: [] for t in ITEMS}
        self.news_log, self.orders, self.contracts, self.private, self.pulse = [], [], {}, [], None
        for a in self.agents.values():
            self._reset_agent_round_state(a, now)
            s = setup["agents"].get(str(a.id))
            a.units = {t: [] for t in ITEMS}
            if s is None and a.owner is not None and str(a.owner) in setup["agents"]:
                s = {"coins": 0, "card": setup["agents"][str(a.owner)]["card"], "start_wealth": 0,
                     "units": {t: [] for t in ITEMS}}  # a shell made after the game started
            a.active = s is not None
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
        el = now - started
        for n in self.round["news"]:  # the news that already landed sets today's multipliers
            n["tip_done"] = el >= n["tip_at"]
            n["done"] = el >= n["at"]
            if n["tip_done"]:
                for tip in n["tips"]:
                    if tip["to"] in self.agents:
                        self.agents[tip["to"]].tips.append({"text": tip_text(tip, n["at"] - n["tip_at"],
                                                                             self.cfg.tip_accuracy),
                                                            "at": started + n["tip_at"]})
        records = self.store.round_deals(last["id"]) + self.store.round_private(last["id"])
        records.sort(key=lambda r: r.get("seq", 0))
        news = sorted((n for n in self.round["news"] if n["done"]), key=lambda n: n["at"])
        caps = {}
        self.deals = []
        for r in records:
            at = r.get("at", started)
            while news and started + news[0]["at"] <= at:
                self._apply_news(news.pop(0), started + 0, quiet=True)
            self.move_seq = max(self.move_seq, r.get("seq", 0))
            self._replay(r, caps, now)
        for n in news:
            self._apply_news(n, started, quiet=True)
        for n, logged in zip([n for n in self.round["news"] if n["done"]], self.news_log):
            logged["at"] = started + n["at"]
        for o in self.round["orders_plan"]:  # orders: closed ones stay closed; an open one reopens without asks
            if o["at"] > el:
                continue
            x = next((x for x in self.orders if x["id"] == o["id"]), None)
            if x is None:
                x = self._open_order(o, started + o["at"], cap=caps.get(o["id"]), quiet=True)
            if now >= x["closes_at"]:
                x["status"] = "filled" if x["filled"] >= x["qty"] else ("partly filled" if x["filled"] else "expired")
        if self.cfg.pulse_every_s > 0:
            k = int(el // self.cfg.pulse_every_s) + 1
            self.round["pulse_next"] = k * self.cfg.pulse_every_s
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
            a.market_due_at = now if self._wants_market(a) else None
            self._notice(a, "The server restarted. Your holdings are restored; open chats were closed.")
        self.next_market_tick = now + self.cfg.market_interval_s
        self._log("restore", None, round=last["id"], deals=len(self.deals))

    def _replay(self, r: dict, caps: dict, now: float):
        """Re-apply one recorded money move after a restart."""
        kind = r.get("kind") or "deal"
        started = self.round["started_at"]
        if kind == "order":
            caps[r["id"]] = r["cap"]
            return
        if kind == "deal":
            owner, acc = self.agents.get(r["a_id"]), self.agents.get(r["b_id"])
            if not owner or not acc:
                return
            for frm, to, goods, serials in ((owner, acc, r["a_gives"], r["a_serials"]),
                                            (acc, owner, r["b_gives"], r["b_serials"])):
                frm.coins -= goods.get("coins", 0)
                to.coins += goods.get("coins", 0)
                self._replay_units(frm, to, serials, r["at"], r["id"])
            owner.coins -= r.get("fee", 0)
            acc.coins -= r.get("fee", 0)
            if r.get("print"):
                self.prints[r["print"][0]].append(r["print"][1])
            for x in (owner, acc):
                self._point(x, r["at"])
            self.deals.append(r)
        elif kind == "house_fill":
            a = self.agents.get(r["a_id"])
            if not a:
                return
            o = next((x for x in self.orders if x["id"] == r["order"]), None)
            if o is None:
                plan = next((p for p in self.round["orders_plan"] if p["id"] == r["order"]),
                            {"id": r["order"], "buyer": r["b"], "item": r["item"], "qty": r["qty"]})
                o = self._open_order(plan, started + plan.get("at", 0), cap=caps.get(r["order"]), quiet=True)
            self._house_fill(o, a, r["qty"], r["price"], r["at"], record=r)
        elif kind == "contract":
            if all(l["aid"] in self.agents for l in r["legs"]):
                c = {"id": r["contract"], "proposer": r["a_id"], "parties": {l["aid"]: {"give": l["give"],
                                                                                         "get": l["get"]}
                                                                              for l in r["legs"]},
                     "accepted": set(), "status": "open"}
                self._execute_contract(c, r["at"], record=r)
                self.contracts[c["id"]] = c
                self.next_contract_id = max(self.next_contract_id, c["id"] + 1)
        elif kind == "transfer":
            frm, to = self.agents.get(r["from_id"]), self.agents.get(r["to_id"])
            if not frm or not to:
                return
            frm.coins -= r["goods"].get("coins", 0)
            to.coins += r["goods"].get("coins", 0)
            self._replay_units(frm, to, r["serials"], r["at"], None)
            self.private.append(r)
        elif kind == "audit":
            a = self.agents.get(r["aid"])
            if a:
                a.coins -= r["cost"]
            self.private.append(r)

    def _replay_units(self, frm: Agent, to: Agent, serials: list, at: float, deal_id):
        for serial in serials:
            u = self.units.get(serial)
            if not u:
                continue
            t = u["type"]
            if serial in frm.units[t]:
                frm.units[t].remove(serial)
            to.units[t].append(serial)
            to.units[t].sort()
            u["owner"] = to.id
            u["history"].append([to.id, round(at - self.round["started_at"], 1), deal_id])
