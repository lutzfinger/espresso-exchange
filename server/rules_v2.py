"""Version 2 rules: pure helpers with no game state (news plan, headlines, ticker math, names).

The market engine (market.py) owns every state change; these functions only compute."""
from __future__ import annotations

import re
import unicodedata

from .items import FULL_NAMES, GOODS, ITEMS

BASE_VALUE = 80            # mean card value (10, 30, 60, 100, 200): the house price before any trades print
MULT_MIN, MULT_MAX = 0.25, 4.0
FACTORS_UP = (1.5, 2.0)
FACTORS_DOWN = (0.5, 0.7)
CAFES = ["Cafe Uno", "Corner Cafe", "Cafe Nord", "Little Crema", "Cafe Lumen", "Roastery Nine", "Cafe Aurora",
         "Kiosk 42", "The Night Owl", "Cafe Meridian"]
HEADLINES = {
    "beans": {"up": ["Frost hits the Gesha farms", "A celebrity chef swears by Gesha", "Gesha shortage"],
              "down": ["A bumper Gesha harvest", "Gesha shows up in every supermarket"]},
    "grinder": {"up": ["A grinder factory burns down", "Burr Grinders go viral online"],
                "down": ["A cheap grinder floods the market", "Grinder glut at the port"]},
    "machine": {"up": ["Import ban on espresso machines", "The Beast wins a design award"],
                "down": ["Machine recall: a faulty boiler", "A new machine makes The Beast look old"]},
    "barista": {"up": ["Barista poaching war", "Star Baristas win the world cup"],
                "down": ["A barista school graduates 500 students", "Robot baristas arrive"]},
    "course": {"up": ["Latte art is the new hobby of the year", "Courses sell out across town"],
               "down": ["Free latte art videos online", "The course teacher moves abroad"]},
}


def fmt_factor(f: float) -> str:
    return "x" + (f"{f:g}")


def news_offsets(length_s: float, n: int) -> list[float]:
    """When the news items land, in seconds after the round start: 24%, 44%, 64%, 84% for four."""
    if n <= 0 or length_s <= 0:
        return []
    if n == 1:
        return [round(length_s * 0.5, 1)]
    return [round(length_s * (0.24 + 0.6 * i / (n - 1)), 1) for i in range(n)]


def headline(item: str, factor: float, rng) -> str:
    kind = "up" if factor > 1 else "down"
    return f"{rng.choice(HEADLINES[item][kind])}: {FULL_NAMES[item]} now {fmt_factor(factor)}"


def plan_news(rng, length_s: float, n: int, lead_s: float, team_ids: list, tips_per_news: int,
              accuracy: float) -> list[dict]:
    """The whole round's news, decided at the start so it survives a restart."""
    plan, last = [], None
    for i, at in enumerate(news_offsets(length_s, n)):
        item = rng.choice([t for t in ITEMS if t != last])
        last = item
        factor = rng.choice(FACTORS_UP + FACTORS_DOWN)
        tips = []
        for aid in rng.sample(list(team_ids), min(max(0, tips_per_news), len(team_ids))):
            true = rng.random() < accuracy
            t_item, t_factor = item, factor
            if not true:
                if rng.random() < 0.5:
                    t_item = rng.choice([t for t in ITEMS if t != item])
                else:
                    t_factor = rng.choice(FACTORS_DOWN if factor > 1 else FACTORS_UP)
            tips.append({"to": aid, "true": true, "item": t_item, "factor": t_factor})
        plan.append({"i": i, "at": at, "tip_at": round(max(5.0, at - lead_s), 1), "item": item, "factor": factor,
                     "headline": headline(item, factor, rng), "tips": tips, "done": False, "tip_done": False})
    return plan


def tip_text(tip: dict, lead_s: float, accuracy: float) -> str:
    mins = max(1, round(lead_s / 60))
    return (f"PRIVATE TIP from the house (tips are right {round(accuracy * 100)}% of the time): in about {mins} "
            f"minute{'s' if mins != 1 else ''} the news will move {FULL_NAMES[tip['item']]} {fmt_factor(tip['factor'])}.")


def plan_orders(rng, first_s: float, every_s: float, window_s: float, until_s: float, qty: int) -> list[dict]:
    if every_s <= 0:
        return []
    out, at, i = [], first_s, 1
    while at + window_s <= until_s:
        out.append({"id": i, "at": round(at, 1), "item": rng.choice(ITEMS), "buyer": rng.choice(CAFES), "qty": qty,
                    "premium": round(rng.uniform(1.3, 2.0), 1)})  # the cafe pays above market: real new demand
        at += every_s
        i += 1
    return out


def ticker_view(prints: dict) -> dict:
    """prints: item -> [price, ...] in time order. Last price and the average of the last five."""
    out = {}
    for t in ITEMS:
        p = prints.get(t) or []
        last5 = p[-5:]
        out[t] = {"last": p[-1] if p else None, "avg5": round(sum(last5) / len(last5)) if last5 else None,
                  "trades": len(p)}
    return out


def ticker_print(give: dict, get: dict):
    """(item, unit price) if a deal is coins for exactly one item type, else None."""
    for coins_side, item_side in ((give, get), (get, give)):
        if set(coins_side) == {"coins"} and len(item_side) == 1 and "coins" not in item_side:
            (item, q), = item_side.items()
            if q > 0:
                return item, round(coins_side["coins"] / q)
    return None


def contract_errors(parties: dict) -> list[str]:
    """parties: name -> {"give": {...}, "get": {...}}. Everything given must be received, good by good."""
    errs = []
    for g in GOODS:
        given = sum(p["give"].get(g, 0) for p in parties.values())
        got = sum(p["get"].get(g, 0) for p in parties.values())
        if given != got:
            errs.append(f"{g}: parties give {given} but get {got}; every good given must be received")
    for name, p in parties.items():
        if not p["give"] and not p["get"]:
            errs.append(f"{name} neither gives nor gets anything")
    return errs


# ---------------------------------------------------------------------- people
PERSON_OK = re.compile(r"^[A-Za-z][A-Za-z '\-.]*[A-Za-z.]$")


def clean_person(raw) -> tuple[str, str]:
    """(display name, comparison key) for one team member, or ValueError with a plain reason.
    Latin letters only after accents are stripped, so look-alike letters cannot dodge the one-team rule."""
    s = unicodedata.normalize("NFKC", str(raw or ""))
    s = re.sub(r"\s+", " ", s).strip()
    if not s:
        raise ValueError("an empty member name")
    plain = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
    if not 3 <= len(s) <= 60:
        raise ValueError(f"'{s[:60]}' must be 3 to 60 characters")
    if not PERSON_OK.match(plain):
        raise ValueError(f"'{s}' has characters the game cannot use: type the name in plain Latin letters")
    words = [w for w in re.split(r"[ \-]", plain) if re.search(r"[A-Za-z]", w)]
    if len(words) < 2:
        raise ValueError(f"'{s}' needs a first and a last name")
    key = re.sub(r"\s+", " ", re.sub(r"[^a-z ]", " ", plain.lower())).strip()
    return s, key


def split_members(raw) -> list:
    if raw is None:
        return []
    if isinstance(raw, str):
        return [p for p in re.split(r"[\n,;]+", raw) if p.strip()]
    if isinstance(raw, (list, tuple)):
        out = []
        for p in raw:
            if isinstance(p, dict):
                p = p.get("name") or " ".join(str(p.get(k, "")) for k in ("first", "last"))
            if p is not None and str(p).strip():
                out.append(str(p))
        return out
    return [str(raw)]
