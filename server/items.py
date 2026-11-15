"""Items, card values, and forgiving parsing of goods/offers."""
from __future__ import annotations

import json
import math
import re

ITEMS = ["beans", "grinder", "machine", "barista", "course"]
FULL_NAMES = {
    "beans": "Gesha Beans",
    "grinder": "Burr Grinder",
    "machine": "Espresso Machine",
    "barista": "Star Barista",
    "course": "Latte Art Course",
}
FLAVOR = {
    "beans": "A bag so rare it has its own fan club",
    "grinder": "Grinds finer than your excuses",
    "machine": "40 kg of chrome, nicknamed The Beast",
    "barista": "A one-season contract with a barista who will not stop talking about extraction",
    "course": "Learn to pour a swan, mostly pour a blob",
}
SERIAL_PREFIX = {t: t.upper() for t in ITEMS}
GOODS = ITEMS + ["coins"]

_ALIASES = {
    "coins": ["coin", "coins", "money", "cash", "c", "gold", "credits"],
    "beans": ["bean", "beans", "gesha", "gesha bean", "gesha beans", "coffee", "coffee beans", "bag"],
    "grinder": ["grinder", "grinders", "burr", "burr grinder", "burr grinders"],
    "machine": ["machine", "machines", "espresso machine", "espresso machines", "espresso", "the beast", "beast"],
    "barista": ["barista", "baristas", "star barista", "star baristas"],
    "course": ["course", "courses", "latte art course", "latte art courses", "latte art", "latte course", "art course"],
}
ALIAS = {a: k for k, names in _ALIASES.items() for a in names}


def norm_good(name) -> str | None:
    """Map 'Burr Grinder', 'grinders', 'COINS' ... to an API name, or None."""
    if name is None:
        return None
    s = re.sub(r"[^a-z ]", " ", str(name).lower())
    s = re.sub(r"\s+", " ", s).strip()
    if s in ALIAS:
        return ALIAS[s]
    for word in s.split(" "):
        if word in ALIAS and ALIAS[word] != "coins":
            return ALIAS[word]
    return None


def _norm_side(side, label: str, errors: list) -> dict:
    out: dict = {}
    if side in (None, "", [], "null", "none"):
        return out
    if isinstance(side, str):
        try:
            side = json.loads(side)
        except ValueError:
            errors.append(f"'{label}' must be an object like {{\"grinder\": 1}}")
            return out
    if isinstance(side, list):  # [{"item":"grinder","qty":1}] or ["grinder"]
        merged: dict = {}
        for el in side:
            if isinstance(el, dict):
                k = el.get("item") or el.get("good") or el.get("type") or el.get("name")
                q = el.get("qty", el.get("quantity", el.get("count", el.get("amount", 1))))
                if k is not None:
                    merged[k] = q
            elif isinstance(el, str):
                merged[el] = merged.get(el, 0) + 1
        side = merged
    if not isinstance(side, dict):
        errors.append(f"'{label}' must be an object like {{\"grinder\": 1}}")
        return out
    for k, v in side.items():
        key = norm_good(k)
        if key is None:
            errors.append(f"unknown good '{k}' (use beans, grinder, machine, barista, course, coins)")
            continue
        try:
            f = float(v)
            if not math.isfinite(f) or abs(f) > 1e9:
                raise ValueError
            q = int(round(f))
        except (TypeError, ValueError, OverflowError):
            errors.append(f"quantity for '{k}' must be a number")
            continue
        if q < 0:
            errors.append(f"negative quantity for '{k}'")
            continue
        if q == 0:
            continue
        out[key] = out.get(key, 0) + q
    return out


def normalize_offer(raw):
    """Return (offer, errors). offer = {'give': {...}, 'get': {...}} or None."""
    errors: list = []
    if raw in (None, "", {}, "null", "none"):
        return None, ["no offer given"]
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None, ["offer must be an object with 'give' and 'get'"]
    if not isinstance(raw, dict):
        return None, ["offer must be an object with 'give' and 'get'"]
    give = _norm_side(raw.get("give", raw.get("you_give")), "give", errors)
    get = _norm_side(raw.get("get", raw.get("you_get")), "get", errors)
    # net out goods named on both sides
    for k in list(give):
        if k in get:
            n = min(give[k], get[k])
            give[k] -= n
            get[k] -= n
            if give[k] == 0:
                del give[k]
            if get[k] == 0:
                del get[k]
    if not give or not get:
        errors.append("an offer must give something and get something")
        return None, errors
    return {"give": give, "get": get}, errors


def value(card: dict, goods: dict) -> int:
    return sum(q if k == "coins" else card.get(k, 0) * q for k, q in goods.items())


def fmt_goods(goods: dict) -> str:
    if not goods:
        return "nothing"
    parts = []
    for k in ITEMS + ["coins"]:
        if k in goods:
            parts.append(f"{goods[k]} {k}")
    return " + ".join(parts)


def fmt_offer(offer: dict | None) -> str:
    if not offer:
        return ""
    return f"{fmt_goods(offer['give'])} for {fmt_goods(offer['get'])}"


def norm_goods(side, label: str = "goods"):
    """Return (goods, errors) for one side, e.g. {"Burr Grinder": "2", "cash": 40} -> {"grinder": 2, "coins": 40}."""
    errors: list = []
    return _norm_side(side, label, errors), errors
