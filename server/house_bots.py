"""Scripted house bots: no LLM, so practice costs nothing.

A Brain is a pure function of the public event format (event dict in, answer dict out). The
server runs four of them in-process; the test suite runs the same brains as external bots
over HTTP, which exercises exactly what a team bot sees.
"""
from __future__ import annotations

import math
import random
import re

from .items import ALIAS, FULL_NAMES, ITEMS, fmt_goods, value

ITEM_BY_NAME = {v.lower(): k for k, v in FULL_NAMES.items()}
TIP = re.compile(r"will move (.+?) x(\d+(?:\.\d+)?)")

HOUSE = {"Fair Trader", "Hardliner", "Wildcard", "Opener"}

STYLES = {
    "Fair Trader": dict(truthful=True, accept_min=10, accept_pct=0.0, sell_markup=1.3, sell_margin=20,
                        buy_ratio=0.7, ideal_pct=0.35, ideal_min=30, concede="half", patience=8,
                        open_per_market=2, max_open=5, post_every=2, opener=False, threats=False, rumors=False),
    "Hardliner": dict(truthful=True, accept_min=40, accept_pct=0.25, sell_markup=2.0, sell_margin=50,
                      buy_ratio=0.5, ideal_pct=1.0, ideal_min=60, concede=0.15, patience=7,
                      open_per_market=1, max_open=4, post_every=3, opener=False, threats=True, rumors=False),
    "Wildcard": dict(truthful=False, accept_min=15, accept_pct=0.0, sell_markup=1.6, sell_margin=30,
                 buy_ratio=0.6, ideal_pct=0.6, ideal_min=40, concede=0.3, patience=8,
                 open_per_market=1, max_open=4, post_every=1, opener=False, threats=False, rumors=True),
    "Opener": dict(truthful=True, accept_min=10, accept_pct=0.0, sell_markup=1.4, sell_margin=25,
                   buy_ratio=0.65, ideal_pct=0.4, ideal_min=30, concede=0.4, patience=8,
                   open_per_market=6, max_open=12, post_every=4, opener=True, threats=False, rumors=False),
}

BUY_WORDS = re.compile(r"\b(buy|buying|want|wants|need|needs|looking for|interested in|after|seeking|pay you|"
                       r"purchase|get your|take your)\b")
SELL_WORDS = re.compile(r"\b(sell|selling|sale|have|got|spare|offering|stock|unload|give you|part with)\b")
ASK_HOLD = re.compile(r"(what|which).{0,20}(have|hold|holding|got|selling|sell|carry|inventory|items)|"
                      r"your (items|holdings|inventory|stock)|how many")


def round5_up(x: float) -> int:
    return int(math.ceil(x / 5.0) * 5)


def round5_down(x: float) -> int:
    return int(math.floor(x / 5.0) * 5)


def items_in(text: str) -> list[tuple[int, str]]:
    """(position, item) for every item mentioned in text, in order."""
    t = text.lower()
    found = []
    for alias, good in ALIAS.items():
        if good == "coins" or len(alias) < 4:
            continue
        for m in re.finditer(r"\b" + re.escape(alias) + r"\b", t):
            found.append((m.start(), good))
    found.sort()
    out, seen = [], set()
    for pos, g in found:
        if g not in seen:
            seen.add(g)
            out.append((pos, g))
    return out


def parse_intent(text: str) -> dict:
    """Very rough read of what the other side said."""
    t = (text or "").lower()
    out = {"they_buy": None, "they_sell": None, "asks_holdings": bool(ASK_HOLD.search(t))}
    for pos, item in items_in(t):
        window = t[max(0, pos - 40): pos + 25]
        b, s = BUY_WORDS.search(window), SELL_WORDS.search(window)
        if b and (not s or b.start() <= s.start()):
            out["they_buy"] = out["they_buy"] or item
        elif s:
            out["they_sell"] = out["they_sell"] or item
    return out


class Brain:
    def __init__(self, style: str, rng: random.Random | None = None):
        self.style = style
        self.s = STYLES[style]
        self.rng = rng or random.Random()
        self.mem: dict = {}
        self.round = None
        self.opened_with: set = set()
        self.markets = 0

    # ---------------------------------------------------------------- numbers
    def threshold(self, ref: int) -> int:
        return int(max(self.s["accept_min"], self.s["accept_pct"] * ref))

    def ideal(self, ref: int) -> float:
        return max(self.s["ideal_min"], self.s["ideal_pct"] * ref)

    def sell_price(self, v: int) -> int:
        return round5_up(max(v * self.s["sell_markup"], v + self.s["sell_margin"]))

    def buy_price(self, v: int) -> int:
        return max(5, round5_down(v * self.s["buy_ratio"]))

    # ---------------------------------------------------------------- talk
    def line(self, kind: str, **kw) -> str:
        S = self.style
        lines = {
            "accept": {"Fair Trader": "Deal. Fair price, thank you.", "Hardliner": "Fine. Deal.",
                       "Wildcard": "Deal! You got the last one at that price.", "Opener": "Deal, pleasure doing business!"},
            "counter": {"Fair Trader": "Let's meet halfway: {o}.",
                        "Hardliner": "{o}. That is already generous. Take it or I walk.",
                        "Wildcard": "Two other traders offered me more. {o}, today only.",
                        "Opener": "Close. How about {o}?"},
            "final": {"Fair Trader": "My best: {o}. Below that I lose Wealth.",
                      "Hardliner": "{o}. Final. Last chance before I walk.",
                      "Wildcard": "{o}. Final, my partner is waiting with cash.",
                      "Opener": "{o} is the best I can do."},
            "sell": {"Fair Trader": "I can sell you {g} for {p} coins. Honest price.",
                     "Hardliner": "{g}: {p} coins. Not a coin less.",
                     "Wildcard": "Last {g} in the market, three buyers lined up. {p} coins.",
                     "Opener": "I have {g} going for {p} coins. Interested?"},
            "buy": {"Fair Trader": "I'd buy {g} for {p} coins. Fair offer.",
                    "Hardliner": "I pay {p} coins for {g}. Take it before I change my mind.",
                    "Wildcard": "Everyone is dumping {g}, prices crashed. I'll still give you {p} coins.",
                    "Opener": "Looking for {g}. {p} coins work for you?"},
            "walk": {"Fair Trader": "No progress, so I'll try elsewhere. Good luck!",
                     "Hardliner": "Waste of my time. I'm walking.",
                     "Wildcard": "Too late, I just sold to someone else.",
                     "Opener": "Let's leave it here. Ping me later!"},
            "missing": {"Fair Trader": "I don't have {g}, sorry.", "Hardliner": "I don't have {g}. Try again.",
                        "Wildcard": "My {g} went to another buyer an hour ago.", "Opener": "I'm out of {g}."},
        }
        return lines[kind][S].format(**kw)

    def holdings_line(self, ev) -> str:
        items = {k: v for k, v in ev["holdings"]["items"].items() if v}
        if self.s["truthful"]:
            if not items:
                return "Honestly, only coins right now."
            return "I hold " + ", ".join(f"{v} {k}" for k, v in items.items()) + "."
        fake = self.rng.sample(ITEMS, 2)
        return f"I'm sitting on {self.rng.randint(2, 4)} {fake[0]} and a {fake[1]}. Plenty for everyone."

    # ---------------------------------------------------------------- offers
    def candidates(self, ev, text: str = ""):
        """Offers worth making, best first: (key, offer, message)."""
        card, items, coins = ev["card"], ev["holdings"]["items"], ev["holdings"]["coins"]
        intent = parse_intent(text)
        out = []

        def sell(t):
            p = self.sell_price(card[t])
            out.append((f"sell:{t}", {"give": {t: 1}, "get": {"coins": p}}, self.line("sell", g=f"1 {t}", p=p)))

        def buy(t):
            p = self.buy_price(card[t])
            if p <= coins:
                out.append((f"buy:{t}", {"give": {"coins": p}, "get": {t: 1}}, self.line("buy", g=f"1 {t}", p=p)))

        tb, ts = intent["they_buy"], intent["they_sell"]
        if tb and items.get(tb, 0) > 0 and card.get(tb, 0) <= 100:
            sell(tb)
        if ts and card.get(ts, 0) >= 60:
            buy(ts)
        sells = sorted((t for t in ITEMS if items.get(t, 0) > 0 and card.get(t, 0) <= 60), key=lambda t: card[t])
        buys = sorted((t for t in ITEMS if card.get(t, 0) >= 100), key=lambda t: -card[t])
        first, second = (sells, buys) if ev.get("chat_id", 0) % 2 == 0 else (buys, sells)
        for i in range(max(len(first), len(second))):
            for lst in (first, second):
                if i < len(lst):
                    (sell if lst is sells else buy)(lst[i])
        seen, uniq = set(), []
        for c in out:
            if c[0] not in seen:
                seen.add(c[0])
                uniq.append(c)
        return uniq

    def default_offer(self, ev, text: str = "", exclude=()):
        for key, offer, msg in self.candidates(ev, text):
            if key not in exclude:
                return offer, msg, key
        return None, None, None

    def counter(self, ev, their: dict, gain: int, thr: int, mem: dict):
        card, coins = ev["card"], ev["holdings"]["coins"]
        we_get, we_give = their["give"], their["get"]
        items_get = {k: v for k, v in we_get.items() if k != "coins"}
        items_give = {k: v for k, v in we_give.items() if k != "coins"}
        goods_net = value(card, items_get) - value(card, items_give)
        ref = max([card.get(k, 0) for k in list(items_get) + list(items_give)] + [10])
        prev = mem.get("target") or self.ideal(ref)
        if self.s["concede"] == "half":
            new = (gain + prev) / 2.0
        else:
            new = prev - self.s["concede"] * (prev - gain)
        final = new <= thr + 5
        new = max(new, thr)
        mem["target"] = new
        net = new - goods_net
        give, get = dict(items_give), dict(items_get)
        if net > 0:
            get["coins"] = round5_up(net)
        elif net < 0:
            pay = round5_down(-net)
            if pay > coins:
                pay = round5_down(coins)
            if pay > 0:
                give["coins"] = pay
        if not give or not get:
            return None, False
        return {"give": give, "get": get}, final

    # ---------------------------------------------------------------- decisions
    def eff_values(self, ev: dict) -> dict:
        """What items are worth to us now (version 2): today's values, raised or lowered by a fresh private tip,
        plus part of the set bonus for the item types our team is missing (and for the last one of a full set)."""
        v = dict(ev.get("values_now") or ev["card"])
        for tip in ev.get("tips") or []:
            m = TIP.search(tip.get("text", ""))
            if m and tip.get("ago_s", 999) <= 150 and ITEM_BY_NAME.get(m.group(1).lower()) in v:
                t = ITEM_BY_NAME[m.group(1).lower()]
                v[t] = round(v[t] * (1 + (float(m.group(2)) - 1) * 0.8))  # tips are right 4 times in 5
        bonus = (ev.get("rules") or {}).get("set_bonus", 0)
        team = ev.get("team")
        if bonus and team:
            held = {t: sum(e["holdings"]["items"].get(t, 0) for e in team["entities"]) for t in ITEMS}
            missing = [t for t in ITEMS if not held[t]]
            if 1 <= len(missing) <= 2:
                for t in missing:
                    v[t] += bonus // (2 * len(missing))
            elif not missing:
                for t in ITEMS:
                    if held[t] == 1:
                        v[t] += bonus // 2
        return v

    def decide(self, ev: dict) -> dict:
        if ev.get("values_now"):  # version 2: what items are worth to us now is what counts
            ev = dict(ev, card=self.eff_values(ev))
        if ev.get("type") == "your_turn":
            out = self.turn(ev)
        elif ev.get("type") == "market":
            out = self.market(ev)
        else:
            return {}
        out.update(self.extras(ev))
        return out

    def extras(self, ev: dict) -> dict:
        """Version 2: sell into house orders above our value, and take contracts that gain us enough."""
        out = {}
        asks = []
        for o in ev.get("house_orders") or []:
            v = o.get("your_value_now") or 0
            if o.get("you_hold", 0) < 1 or o.get("your_ask") is not None:
                continue
            price = min(o["cap"], round5_up(max(v + self.s["sell_margin"], v * self.s["sell_markup"])))
            if price >= v + 10:
                asks.append({"order": o["order"], "price": price, "qty": 1})
        if asks:
            out["house_ask"] = asks
        if self.style in ("Fair Trader", "Opener"):
            replies = [{"contract": c["contract"], "accept": c["wealth_change"] >= self.s["accept_min"]}
                       for c in ev.get("contracts") or [] if not c.get("you_accepted")]
            if replies:
                out["contract_reply"] = replies
        return out

    def turn(self, ev: dict) -> dict:
        me = ev["you"]
        mem = self.mem.setdefault(ev["chat_id"], {"target": None, "last_gain": None, "stuck": 0, "tried": set(),
                                                   "final_sent": False, "talk_only": 0})
        mine = sum(1 for m in ev["transcript"] if m["from"] == me)
        theirs = [m for m in ev["transcript"] if m["from"] not in (me, "SERVER")]
        text = theirs[-1]["text"] if theirs else ""
        their, acc = ev.get("their_offer"), ev.get("if_you_accept")
        out_of_time = mine >= self.s["patience"] or ev["messages_left"] <= 1
        walk = {"action": "end", "message": self.line("walk")}

        def propose(lead=""):
            offer, msg, key = self.default_offer(ev, text, exclude=mem["tried"])
            if not offer:
                return {"action": "end", "message": (lead + " " + self.line("walk")).strip()[:290]}
            mem["tried"].add(key)
            mem["target"] = None
            return {"action": "offer", "message": (lead + " " + msg).strip()[:290], "offer": offer}

        if their and acc:
            fee = (ev.get("rules") or {}).get("trade_fee", 0)
            gain = value(ev["card"], their["give"]) - value(ev["card"], their["get"]) - fee
            ref = max([ev["card"].get(k, 0) for k in list(their["give"]) + list(their["get"]) if k != "coins"]
                      + [10])
            thr = self.threshold(ref)
            if acc["you_can_pay"] and gain >= thr:
                return {"action": "accept", "message": self.line("accept")}
            if out_of_time:
                return walk
            mem["stuck"] = mem["stuck"] + 1 if mem["last_gain"] is not None and gain <= mem["last_gain"] else 0
            mem["last_gain"] = gain
            if (mem["final_sent"] and mem["stuck"] >= 1) or mem["stuck"] >= 3:
                return walk
            if acc["you_can_pay"]:
                offer, final = self.counter(ev, their, gain, thr, mem)
                if offer:
                    mem["final_sent"] = mem["final_sent"] or final
                    kind = "final" if final else "counter"
                    return {"action": "offer", "message": self.line(kind, o=self.describe(offer)), "offer": offer}
                mem["tried"].add("their:" + str(sorted(their["give"].items())))
                return propose("That item is not for me.")
            missing = {k: v for k, v in acc["you_give"].items()
                       if (ev["holdings"]["coins"] if k == "coins" else ev["holdings"]["items"].get(k, 0)) < v}
            return propose(self.line("missing", g=fmt_goods(missing)))
        if out_of_time:
            return walk
        mem["talk_only"] += 1
        if mem["talk_only"] >= 4:
            return walk
        intent = parse_intent(text)
        lead = self.holdings_line(ev) if intent["asks_holdings"] else ""
        mine_offer = ev.get("your_offer")
        if mine_offer and mine and mem["talk_only"] < 2:
            if self.s["concede"] == "half" or self.s["opener"]:
                return {"action": "talk", "message": (lead + f" My offer stands: {self.describe(mine_offer)}.").strip()}
            return {"action": "talk", "message": (lead + " You heard my price. Make an offer or I walk.").strip()}
        if lead or not mine_offer:
            r = propose(lead)
            if r["action"] == "end" and lead:
                return {"action": "talk", "message": lead + " What are you looking for?"}
            return r
        return propose()

    @staticmethod
    def describe(offer: dict) -> str:
        return f"I give {fmt_goods(offer['give'])}, you give {fmt_goods(offer['get'])}"

    def market(self, ev: dict) -> dict:
        self.markets += 1
        if ev.get("round") != self.round:
            self.round = ev.get("round")
            self.opened_with = set()
            self.mem = {}
        open_partners = {c["partner"] for c in ev.get("open_chats", [])}
        room = self.s["max_open"] - len(open_partners)
        cands = [n for n in ev.get("agents", []) if n not in open_partners]
        if self.s["opener"]:
            targets = [n for n in cands if n not in HOUSE and n not in self.opened_with]
        else:
            wanted = {t for t in ITEMS if ev["card"].get(t, 0) >= 100}
            posters = []
            for p in ev.get("board", []):
                if p["author"] in cands and any(g in wanted for _, g in items_in(p["text"])):
                    posters.append(p["author"])
            others = [n for n in cands if n not in posters]
            self.rng.shuffle(others)
            targets = list(dict.fromkeys(posters + others))
        opens = []
        for i, name in enumerate(targets[:max(0, min(room, self.s["open_per_market"]))]):
            fake = dict(ev, chat_id=self.markets + i)
            offer, msg, _key = self.default_offer(fake)
            hello = "Hi {n}! What are you holding?".format(n=name) if self.s["opener"] else f"Hi {name}."
            opens.append({"to": name, "message": f"{hello} {msg or ''}".strip()[:290], "offer": offer})
            self.opened_with.add(name)
        post = None
        if ev.get("posts_left_this_minute", 1) > 0 and self.markets % self.s["post_every"] == 0:
            post = self.make_post(ev)
        return {"open": opens, "post": post}

    def make_post(self, ev: dict) -> str:
        card, items = ev["card"], ev["holdings"]["items"]
        sell = [t for t in ITEMS if items.get(t, 0) > 0 and card.get(t, 0) <= 60]
        want = [t for t in sorted(ITEMS, key=lambda t: -card.get(t, 0)) if card.get(t, 0) >= 100]
        me = ev.get("you", self.style)
        if self.style == "Fair Trader":
            return (f"{me}: selling {', '.join(sell) or 'nothing right now'}; buying {', '.join(want)}. "
                    f"Honest prices.")
        if self.style == "Hardliner":
            return f"{me} buying {want[0] if want else 'quality'}. Serious sellers only. Lowballers get blocked."
        if self.style == "Opener":
            return f"{me} here: I chat with everyone. Buying {', '.join(want)}, selling {', '.join(sell) or '-'}."
        t = self.rng.choice(ITEMS)
        rumors = [
            f"Insider tip: {t} is worth 10 to almost everyone. Sell yours while you still can.",
            f"Just sold a {t} for {self.rng.choice([350, 400, 450])} coins. Prices are going up!",
            f"HOUSE NOTICE: {t} values double at the bell. Buy now. (Official.)",
            f"Market crash on {t}: three traders are dumping them below 20 coins.",
            f"Nobody has any {t} left. If you hold one, name your price.",
            f"Heard from a friend at the house: big news on {t} in two minutes. You did not hear it from me.",
        ]
        return self.rng.choice(rumors)
