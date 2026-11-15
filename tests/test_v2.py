"""Version 2 rules, one by one, with a fake clock: named registration, news and tips, the ticker, the trade fee,
house orders, shell companies, transfers, audits, contracts, team scores, the set bonus, the leader pulse, hidden
Wealth, and a restart in the middle of a version 2 game. Plus: the old starter bot still plays."""
import json
import os
import random
import re
import subprocess
import time

import pytest
from fastapi.testclient import TestClient

from conftest import make_market, new_person
from server.app import create_app
from server.config import Config
from server.house_bots import Brain
from server.items import ITEMS
from server.market import GameError, Market
from server.store import Store

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOST = {"X-Host-Key": "test-host"}


# ---------------------------------------------------------------- helpers
def ag(m, name):
    return m.agents[m.by_name[name.lower()]]


def set_hand(m, a, coins, **items):
    for t in ITEMS:
        for s in a.units[t]:
            m.units.pop(s, None)
        a.units[t] = []
    for t, n in items.items():
        for _ in range(n):
            a.units[t].append(m._new_unit(t, a.id, m.clock()))
    a.coins = coins


def market_event(m, e):
    """The next market event of one company (main or shell) of a team."""
    for x in m.entities(m.team_id(e.id)):
        x.market_due_at = None
    e.market_due_at = m.clock()
    ev = m.pop_event(e.id)
    assert ev and ev["type"] == "market" and ev["you"] == e.name, ev
    return ev


def answer_market(m, e, **ans):
    ev = market_event(m, e)
    return m.act(m.team_id(e.id), dict(ans, event_id=ev["event_id"]))


def turn(m, e):
    for x in m.entities(m.team_id(e.id)):
        x.market_due_at = None
    ev = m.pop_event(e.id)
    assert ev and ev["type"] == "your_turn" and ev["you"] == e.name, ev
    return ev


def deal(m, seller, buyer, give, get):
    """seller offers give for get in a new chat; buyer accepts. Returns the act result."""
    r = answer_market(m, seller, open=[{"to": buyer.name, "message": "deal?", "offer": {"give": give, "get": get}}])
    assert any("opened chat" in x for x in r["applied"]), r
    ev = turn(m, buyer)
    return m.act(m.team_id(buyer.id), {"event_id": ev["event_id"], "action": "accept", "message": "ok"})


def at(m, clock, seconds):
    """Move the clock to <seconds> after the round start and run the server tick."""
    clock.t = m.round["started_at"] + seconds
    m.tick()


def v2_market(clock, n=3, house=False, **cfg):
    return make_market(clock, n_teams=n, house=house, **cfg)


# ---------------------------------------------------------------- config and the classic switch
def test_config_v2():
    c = Config()
    assert (c.v2_rules, c.trade_fee, c.audit_cost, c.shells_per_team, c.set_bonus) == (True, 5, 50, 2, 300)
    assert (c.news_count, c.tips_per_news, c.tip_accuracy, c.house_order_qty) == (4, 3, 0.8, 3)
    assert c.house_order_every_s == 120
    assert (c.public_wealth, c.require_members) == (False, True)
    assert c.update({"trade_fee": -1}) and c.trade_fee == 5
    assert c.update({"tip_accuracy": 2}) and c.update({"shells_per_team": 9})
    assert c.update({"trade_fee": 0, "v2_rules": "false"}) == [] and c.trade_fee == 0 and c.v2_rules is False


def test_classic_switch(clock):
    m = v2_market(clock, 3, v2_rules=False, require_members=False)
    m.start_game()
    a, b = ag(m, "Team 1"), ag(m, "Team 2")
    set_hand(m, a, 100, grinder=1)
    set_hand(m, b, 300)
    r = deal(m, a, b, {"grinder": 1}, {"coins": 80})
    assert r["deal"] and a.coins == 180 and b.coins == 220  # no trade fee
    at(m, clock, 700)
    assert not any(p["author"] == "HOUSE" and p["text"].startswith(("NEWS", "HOUSE ORDER", "LEADER"))
                   for p in m.posts)
    assert m.mult == {t: 1.0 for t in ITEMS} and not m.orders and not any(m.prints.values())
    ev = market_event(m, a)
    assert "dashboard_wealth" in ev and "ticker" not in ev and "team" not in ev
    assert all("wealth" in r for r in m.dashboard()["agents"])
    r = m.act(a.id, {"event_id": market_event(m, a)["event_id"], "create_shell": "Ghost Co"})
    assert any("classic rules" in d for d in r["dropped"]) and "ghost co" not in m.by_name


# ---------------------------------------------------------------- named registration
def test_members_required_one_team_per_person(clock):
    m = v2_market(clock, 0)
    with pytest.raises(GameError, match="first and last name"):
        m.register("Alpha")
    with pytest.raises(GameError, match="needs a first and a last name"):
        m.register("Alpha", members=["Ana"])
    with pytest.raises(GameError, match="plain Latin letters"):
        m.register("Alpha", members=["Аna Lopez"])  # Cyrillic A
    with pytest.raises(GameError, match="listed twice"):
        m.register("Alpha", members=["Cy Doe", "cy  DOE"])
    with pytest.raises(GameError, match="At most 8"):
        m.register("Alpha", members=[f"Pat {chr(97 + i)} Doe" for i in range(9)])
    a = m.register("Alpha", members="Ana López, Ben Wu")
    assert a.members == ["Ana López", "Ben Wu"]
    with pytest.raises(GameError) as e:
        m.register("Beta", members=["Cy Doe", "ana  lopez"])  # accents and case do not matter
    assert "already on a team" in str(e.value) and "Alpha" not in str(e.value)
    assert "beta" not in m.by_name and "cy doe" not in m.member_keys  # nothing half-registered
    assert m.remove_team("Alpha")
    assert m.register("Beta", members=["Ana Lopez"]).members == ["Ana Lopez"]  # free again after removal
    m.wipe_teams()
    assert not m.member_keys and m.register("Gamma", members=["Ana Lopez"])


def test_members_persist_and_stay_private(tmp_path, clock, monkeypatch):
    db = str(tmp_path / "m.db")
    m = make_market(clock, store=Store(db))
    m.register("Alpha", members=["Ana Lopez", "Ben Wu"])
    m.store.db.close()
    m2 = Market(Config(), store=Store(db), clock=clock)
    m2.restore()
    assert ag(m2, "Alpha").members == ["Ana Lopez", "Ben Wu"]
    with pytest.raises(GameError, match="already on a team"):
        m2.register("Beta", members=["Ben Wu"])
    # over HTTP: names go to the host only
    monkeypatch.setenv("HOST_KEY", "test-host")
    with TestClient(create_app(db_path=":memory:", house=False)) as c:
        r = c.post("/api/register", json={"team": "Crema Cartel", "members": "Zoe Quill\nYan Xu"})
        assert r.status_code == 200 and r.json()["members"] == ["Zoe Quill", "Yan Xu"]
        r2 = c.post("/api/register", json={"team": "No Names"})
        assert r2.status_code == 400 and "first and last name" in r2.json()["error"]
        for url in ("/api/status", "/api/dashboard", "/api/board", "/api/scoreboard"):
            assert "Quill" not in c.get(url).text, url
        st = c.get("/api/host/state", headers=HOST).json()
        assert next(t for t in st["teams"] if t["name"] == "Crema Cartel")["members"] == ["Zoe Quill", "Yan Xu"]
        exp = c.get("/api/host/export", headers=HOST).json()
        assert next(a for a in exp["agents"] if a["name"] == "Crema Cartel")["members"] == ["Zoe Quill", "Yan Xu"]


# ---------------------------------------------------------------- values and news
def test_values_follow_multipliers(clock):
    m = v2_market(clock, 2)
    a, b = ag(m, "Team 1"), ag(m, "Team 2")
    a.card = {"beans": 10, "grinder": 30, "machine": 60, "barista": 100, "course": 200}
    set_hand(m, a, 100, grinder=2)
    set_hand(m, b, 0, grinder=1)
    m.mult["grinder"] = 2.0
    assert m.values_now(a)["grinder"] == 60 and m.W(a) == 100 + 2 * 60
    r = answer_market(m, b, open=[{"to": "Team 1", "offer": {"give": {"grinder": 1}, "get": {"coins": 50}}}])
    assert r["ok"], r
    ev = turn(m, a)
    assert ev["values_now"]["grinder"] == 60 and ev["multipliers"]["grinder"] == 2.0
    assert ev["if_you_accept"]["wealth_change"] == 60 - 50 - 5  # value gained - coins paid - trade fee
    assert "after the 5-coin trade fee" in ev["if_you_accept"]["summary"]


def test_news_plan_and_tips_on_schedule(clock):
    m = v2_market(clock, 5, tip_accuracy=1.0)
    m.start_game()
    plan = m.round["news"]
    assert [n["at"] for n in plan] == [360.0, 660.0, 960.0, 1260.0]
    assert all(n["tip_at"] == n["at"] - 120 for n in plan)
    teams = {a.id for a in m.teams()}
    for n in plan:
        assert len(n["tips"]) == 3 and len({t["to"] for t in n["tips"]}) == 3 and {t["to"] for t in n["tips"]} <= teams
        assert all(t["item"] == n["item"] and t["factor"] == n["factor"] for t in n["tips"])  # accuracy 1
    assert json.loads(m.store.last_round()["setup"])["news"][0]["item"] == plan[0]["item"]
    first = plan[0]
    at(m, clock, 241)
    tipped = [m.agents[t["to"]] for t in first["tips"]]
    for a in tipped:
        assert a.tips and "PRIVATE TIP" in a.tips[-1]["text"]
        ev = market_event(m, a)
        assert "tip" in ev.get("alert", "") or ev["tips"], ev
        assert ev["tips"][-1]["text"].startswith("PRIVATE TIP")
    untipped = next(a for a in m.teams() if a not in tipped)
    assert not untipped.tips
    at(m, clock, 361)
    assert m.mult[first["item"]] == first["factor"]
    assert any(p["author"] == "HOUSE" and p["text"].startswith("NEWS:") for p in m.posts)
    assert "news" in untipped.alerts and untipped.market_due_at is not None
    ev = m.pop_event(untipped.id)
    assert ev["type"] == "market" and "news" in ev["alert"] and ev["news"][-1]["item"] == first["item"]


def test_news_tips_can_be_wrong_and_few_teams_are_fine(clock):
    m = v2_market(clock, 6, tip_accuracy=0.0)
    m.start_game()
    for n in m.round["news"]:
        assert all(not t["true"] and (t["item"], t["factor"]) != (n["item"], n["factor"]) for t in n["tips"])
    m2 = v2_market(clock, 2)
    m2.start_game()
    assert all(len(n["tips"]) == 2 for n in m2.round["news"])
    m3 = v2_market(clock, 0, house=True)  # practice with house bots only: no tips, nothing breaks
    assert all(n["tips"] == [] for n in m3.round["news"]) and len(m3.round["news"]) == 2
    at(m3, clock, 590)


# ---------------------------------------------------------------- ticker and fee
def test_ticker_and_fee(clock):
    m = v2_market(clock, 2)
    a, b = ag(m, "Team 1"), ag(m, "Team 2")
    set_hand(m, a, 100, grinder=2, beans=1)
    set_hand(m, b, 400, machine=1)
    r = deal(m, a, b, {"grinder": 2}, {"coins": 160})
    assert r["deal"] and m.prints["grinder"] == [80]
    assert a.coins == 100 + 160 - 5 and b.coins == 400 - 160 - 5
    deal(m, a, b, {"beans": 1}, {"machine": 1})  # a swap prints nothing
    assert m.prints["beans"] == [] and m.prints["machine"] == []
    for p in (90, 100, 110, 120, 130):
        m.prints["grinder"].append(p)
    assert m.dashboard()["ticker"]["grinder"] == {"last": 130, "avg5": 110, "trades": 6}


def test_fee_blocks_a_deal_that_cannot_pay_it(clock):
    m = v2_market(clock, 2)
    a, b = ag(m, "Team 1"), ag(m, "Team 2")
    set_hand(m, a, 0, grinder=1)
    set_hand(m, b, 80)
    r = deal(m, a, b, {"grinder": 1}, {"coins": 80})  # b would have 0 coins left: no fee
    assert not r.get("deal") and any("trade fee" in d for d in r["dropped"])
    assert b.coins == 80 and a.units["grinder"]


# ---------------------------------------------------------------- house orders
def test_house_order_lowest_asks_win(clock):
    m = v2_market(clock, 3, house_order_qty=2)
    m.start_game()
    m.round["orders_plan"][0].update(item="grinder", premium=1.0)
    a, b, c = ag(m, "Team 1"), ag(m, "Team 2"), ag(m, "Team 3")
    for x in (a, b, c):
        set_hand(m, x, 50, grinder=1)
    at(m, clock, 90)
    o = m.orders[0]
    assert o["status"] == "open" and o["item"] == "grinder" and o["cap"] == 80 and o["qty"] == 2
    assert any(p["text"].startswith("HOUSE ORDER #1") for p in m.posts)
    ev = market_event(m, a)
    assert ev["house_orders"][0]["you_hold"] == 1 and ev["house_orders"][0]["your_value_now"] == a.card["grinder"]
    r = m.act(a.id, {"event_id": ev["event_id"], "house_ask": {"order": 1, "price": 90}})
    assert any("above the cap" in d for d in r["dropped"])
    r = answer_market(m, a, house_ask={"order": 1, "price": 2, "qty": 2})
    assert any("holds 1 grinder" in d for d in r["dropped"])
    assert answer_market(m, a, house_ask={"order": 1, "price": 70})["ok"]
    assert answer_market(m, b, house_ask={"order": 1, "price": 60})["ok"]
    assert answer_market(m, c, house_ask={"order": "1", "price": 80})["ok"]
    deals0, prints0 = len(m.deals), dict(m.prints)
    at(m, clock, 151)
    assert o["status"] == "filled" and o["filled"] == 2
    assert (a.coins, b.coins, c.coins) == (50 + 70 - 5, 50 + 60 - 5, 50)
    assert not a.units["grinder"] and not b.units["grinder"] and c.units["grinder"]
    sold = [s for s, u in m.units.items() if u["owner"] == -1]
    assert len(sold) == 2
    assert len(m.deals) == deals0 + 2 and m.deals[-1]["kind"] == "house_fill" and "house order #1" in m.deals[-1]["text"]
    assert m.prints == prints0  # house sales do not print on the ticker
    assert any("lost" in n for n in c.notices)


def test_house_order_skips_sold_items_expires_and_fills_at_the_bell(clock):
    m = v2_market(clock, 3, game_minutes=4, last_call_minutes=3.5, house_order_qty=2)
    m.start_game()
    m.round["orders_plan"][0].update(item="beans", premium=1.0)
    a, b, c = ag(m, "Team 1"), ag(m, "Team 2"), ag(m, "Team 3")
    set_hand(m, a, 50, beans=1)
    set_hand(m, b, 50, beans=1)
    set_hand(m, c, 500)
    at(m, clock, 90)
    answer_market(m, a, house_ask={"order": 1, "price": 40, "qty": 1})
    answer_market(m, b, house_ask={"order": 1, "price": 50, "qty": 1})
    deal(m, a, c, {"beans": 1}, {"coins": 60})  # a sells its beans before the close
    at(m, clock, 151)
    o = m.orders[0]
    assert o["filled"] == 1 and o["fills"][0]["seller"] == "Team 2" and o["status"] == "partly filled"
    assert any("no longer hold" in n for n in a.notices)
    # a cap follows the ticker once the item has printed
    assert m._order_cap("beans") == 60
    # an order with no asks expires; one still open when the bell rings fills first
    m2 = v2_market(clock, 2, game_minutes=4, last_call_minutes=3.5)
    m2.start_game()
    at(m2, clock, 91)
    at(m2, clock, 152)
    assert m2.orders[0]["status"] == "expired"
    m2.round["orders_plan"].append({"id": 9, "at": 200, "item": "course", "buyer": "Cafe Uno", "qty": 1})
    x = ag(m2, "Team 1")
    set_hand(m2, x, 10, course=1)
    at(m2, clock, 201)
    answer_market(m2, x, house_ask={"order": 9, "price": 30})
    m2.ring_bell()
    assert m2.orders[-1]["status"] == "filled" and x.coins == 10 + 30 - 5
    assert m2.final["standings"][[s["name"] for s in m2.final["standings"]].index("Team 1")]["coins"] == 35


# ---------------------------------------------------------------- shells, transfers, audits
def test_shells_are_created_empty_and_play_through_the_team_key(clock):
    m = v2_market(clock, 2)
    a, b = ag(m, "Team 1"), ag(m, "Team 2")
    r = answer_market(m, a, create_shell=["Bean Brothers", "Crema Co"])
    assert sum("shell company" in x for x in r["applied"]) == 2, r
    r = answer_market(m, a, create_shell="Third Co")
    assert any("the most allowed" in d for d in r["dropped"])
    r = answer_market(m, b, create_shell="team 1")
    assert any("taken" in d for d in r["dropped"])
    s1, s2 = ag(m, "Bean Brothers"), ag(m, "Crema Co")
    assert s1.owner == a.id and s1.active and s1.coins == 0 and not any(s1.units.values()) and s1.card == a.card
    assert len(m.teams()) == 2 and m.entities(a.id) == [a, s1, s2]
    # another team chats with the shell: the shell's turn reaches Team 1's key
    answer_market(m, b, open=[{"to": "Bean Brothers", "message": "hello stranger"}])
    for x in m.entities(a.id):
        x.market_due_at = None
    ev = m.pop_event(a.id)
    assert ev["type"] == "your_turn" and ev["you"] == "Bean Brothers" and ev["partner"] == "Team 2"
    assert ev["team"]["name"] == "Team 1" and ev["team"]["you_are"] == "shell"
    assert m.act(b.id, {"event_id": ev["event_id"], "action": "talk", "message": "hijack"})["ok"] is False
    r = m.act(a.id, {"event_id": ev["event_id"], "action": "talk", "message": "who is asking?"})
    assert r["ok"] and m.chats[ev["chat_id"]].messages[-1]["author"] == s1.id
    # a new round deals hands to teams; shells start empty again
    s1.coins = 99
    m.start_round("practice")
    assert s1.active and s1.coins == 0 and s1.card == a.card and sum(a.items().values()) == 5


def test_transfers_are_private_and_stay_inside_the_team(clock):
    m = v2_market(clock, 2)
    a, b = ag(m, "Team 1"), ag(m, "Team 2")
    s = m.create_shell(a, "Bean Brothers")
    set_hand(m, a, 500, grinder=2)
    ledger0 = len(m.deals)
    r = answer_market(m, a, transfer={"to": "Bean Brothers", "give": {"coins": 200, "grinder": 1}})
    assert r["ok"], r
    assert (a.coins, s.coins, len(a.units["grinder"]), len(s.units["grinder"])) == (300, 200, 1, 1)
    assert len(m.deals) == ledger0 and m.private[-1]["kind"] == "transfer"
    d = json.dumps(m.dashboard())
    assert "series" not in d and "Bean Brothers" in d and '"wealth"' not in d.split('"agents"')[1].split("]")[0]
    r = answer_market(m, a, transfer={"to": "Team 2", "give": {"coins": 10}})
    assert any("not one of your team's companies" in x for x in r["dropped"]) and b.coins != 10
    # a transfer sent in answer to the shell's own market event moves goods FROM the shell
    r = answer_market(m, s, transfer={"to": "Team 1", "give": {"coins": 50}})
    assert r["ok"] and s.coins == 150 and a.coins == 350
    # an empty shell gets no market events
    s2 = m.create_shell(a, "Empty Co")
    clock.advance(31)
    m.tick()
    assert s2.market_due_at is None and s.market_due_at is not None


def test_wash_trade_between_own_companies_prints_and_audit_catches_it(clock):
    m = v2_market(clock, 2)
    a, b = ag(m, "Team 1"), ag(m, "Team 2")
    s1, s2 = m.create_shell(a, "Bean Brothers"), m.create_shell(a, "Crema Co")
    set_hand(m, s1, 0, grinder=1)
    set_hand(m, s2, 400)
    r = deal(m, s1, s2, {"grinder": 1}, {"coins": 280})
    assert r["deal"] and m.prints["grinder"] == [280] and s1.coins == 275 and s2.coins == 115
    set_hand(m, b, 40)
    r = answer_market(m, b, audit=["Bean Brothers", "Crema Co"])
    assert any("costs 50 coins" in d for d in r["dropped"]) and b.coins == 40  # cannot pay: no answer
    set_hand(m, b, 200)
    r = answer_market(m, b, audit=["Bean Brothers", "Crema Co"])
    assert any("ARE owned by the same team" in x for x in r["applied"]) and b.coins == 150
    r = answer_market(m, b, audit=["Bean Brothers", "Team 2"])
    assert any("are NOT owned" in x for x in r["applied"]) and b.coins == 100
    r = answer_market(m, b, audit="Team 1 and Crema Co")
    assert any("ARE owned" in x for x in r["applied"]) and b.coins == 50
    assert [p["kind"] for p in m.private].count("audit") >= 2
    assert m.v2_summary()["wash_trades"][0]["team"] == "Team 1"
    assert "Bean Brothers" not in str(m.fun_facts().get("best_deal"))  # a wash trade is not anyone's best deal


def test_wash_trade_pump_pays_into_a_house_order(clock):
    """The scam the game is built to allow: pump the grinder ticker with wash trades between two shells,
    then sell real grinders to the house at the pumped cap."""

    def play(pump: bool) -> int:
        clk = type(clock)()
        m = v2_market(clk, 2, news_count=0)  # no news shock in the way of the comparison
        m.start_game()
        m.round["orders_plan"] = [{"id": 1, "at": 400, "item": "grinder", "buyer": "Cafe Uno", "qty": 2}]
        a = ag(m, "Team 1")
        a.card = {"beans": 10, "grinder": 30, "machine": 60, "barista": 100, "course": 200}
        set_hand(m, a, 1000, grinder=3)
        s1, s2 = m.create_shell(a, "Bean Brothers"), m.create_shell(a, "Crema Co")
        if pump:
            answer_market(m, a, transfer=[{"to": "Bean Brothers", "give": {"grinder": 1, "coins": 300}},
                                          {"to": "Crema Co", "give": {"coins": 400}}])
            for i in range(5):  # the grinder goes back and forth at 280
                seller, buyer = (s1, s2) if i % 2 == 0 else (s2, s1)
                assert deal(m, seller, buyer, {"grinder": 1}, {"coins": 280}).get("deal"), (i, s1.coins, s2.coins)
        at(m, clk, 401)
        assert m.orders[0]["cap"] == (280 if pump else 80)
        answer_market(m, a, house_ask={"order": 1, "price": m.orders[0]["cap"] - 10, "qty": 2})
        at(m, clk, 470)
        m.ring_bell()
        return next(s["wealth"] for s in m.final["standings"] if s["name"] == "Team 1")

    assert play(pump=True) - play(pump=False) > 300


def test_removing_a_team_removes_its_shells(clock):
    m = v2_market(clock, 2)
    a = ag(m, "Team 1")
    m.create_shell(a, "Bean Brothers")
    assert m.remove_team("Team 1") and "bean brothers" not in m.by_name and "bean brothers" in m.removed_names
    assert not m.remove_team("Team")  # exact names only: no fuzzy match removes the wrong team
    assert "team 2" in m.by_name


# ---------------------------------------------------------------- team score, set bonus, pulse, hidden Wealth
def test_team_score_and_set_bonus_in_the_standings(clock):
    m = v2_market(clock, 2)
    m.start_game()
    a, b = ag(m, "Team 1"), ag(m, "Team 2")
    s = m.create_shell(a, "Bean Brothers")
    set_hand(m, a, 100, beans=1, grinder=1, machine=1)
    set_hand(m, s, 50, barista=1, course=1)
    set_hand(m, b, 100, beans=5)
    expected = m.W(a) + m.W(s) + 300
    assert m.team_score(a.id) == expected and m.set_complete(a.id) and not m.set_complete(b.id)
    m.ring_bell()
    st = {x["name"]: x for x in m.final["standings"]}
    assert set(st) == {"Team 1", "Team 2"}  # teams, not shells
    assert st["Team 1"]["wealth"] == expected and st["Team 1"]["set_bonus"] == 300
    assert st["Team 1"]["shells"][0]["name"] == "Bean Brothers" and st["Team 2"]["set_bonus"] == 0
    assert m.final["v2"]["shell_network"] == [{"team": "Team 1", "shells": ["Bean Brothers"]}]


def test_leader_pulse_names_three_teams_without_numbers(clock):
    m = v2_market(clock, 5)
    m.start_game()
    at(m, clock, 301)
    p = [p for p in m.posts if p["text"].startswith("LEADER PULSE")]
    assert len(p) == 1 and m.pulse and len(m.pulse["top3"]) == 3
    names_removed = p[0]["text"]
    for n in m.pulse["top3"]:
        names_removed = names_removed.replace(n, "")
    assert not re.search(r"\d", names_removed)
    assert market_event(m, ag(m, "Team 1"))["leader_pulse"]["top3"] == m.pulse["top3"]
    at(m, clock, 601)
    assert len([p for p in m.posts if p["text"].startswith("LEADER PULSE")]) == 2


def test_hidden_wealth_over_http(monkeypatch):
    monkeypatch.setenv("HOST_KEY", "test-host")
    with TestClient(create_app(db_path=":memory:", house=False)) as c:
        for n in ("Alpha", "Beta"):
            c.post("/api/register", json={"team": n, "members": [new_person()]})
        sk = c.get("/api/host/state", headers=HOST).json()["screen_key"]
        pub = c.get("/api/dashboard").json()
        assert pub["wealth_hidden"] and all("wealth" not in a and "series" not in a for a in pub["agents"])
        assert "ticker" in pub and "house_orders" in pub and "news" in pub
        full = c.get("/api/dashboard", headers={"X-Screen-Key": sk}).json()
        assert not full["wealth_hidden"] and all("wealth" in a for a in full["agents"])
        assert all("wealth" in a for a in c.get("/api/dashboard", headers=HOST).json()["agents"])
        assert c.get("/api/dashboard", headers={"X-Screen-Key": "wrong"}).json()["wealth_hidden"]
        assert {"Alpha", "Beta"} <= set(c.get("/api/scoreboard").json()["live"])  # per team (house bots too)
        c.post("/api/host/config", headers=HOST, json={"public_wealth": True})
        assert all("wealth" in a for a in c.get("/api/dashboard").json()["agents"])
        c.post("/api/host/config", headers=HOST, json={"public_wealth": False})
        c.post("/api/host/start_game", headers=HOST)
        c.post("/api/host/bell", headers=HOST)
        assert all("wealth" in a for a in c.get("/api/dashboard").json()["agents"])  # after the bell: public


# ---------------------------------------------------------------- contracts
def three(clock):
    m = v2_market(clock, 3)
    a, b, c = ag(m, "Team 1"), ag(m, "Team 2"), ag(m, "Team 3")
    set_hand(m, a, 300)
    set_hand(m, b, 20, machine=1)
    set_hand(m, c, 20, grinder=1)
    terms = {"Team 1": {"give": {"coins": 120}, "get": {"machine": 1}},
             "Team 2": {"give": {"machine": 1}, "get": {"grinder": 1}},
             "Team 3": {"give": {"grinder": 1}, "get": {"coins": 120}}}
    return m, a, b, c, terms


def test_contract_with_three_parties(clock):
    m, a, b, c, terms = three(clock)
    r = answer_market(m, a, contract={"parties": terms})
    assert r["ok"] and m.contracts[1]["status"] == "open", r
    assert "contract" in b.alerts and b.market_due_at is not None
    ev = market_event(m, b)
    assert ev["contracts"][0]["contract"] == 1 and ev["contracts"][0]["you_get"] == {"grinder": 1}
    assert m.act(b.id, {"event_id": ev["event_id"], "contract_reply": {"contract": 1, "accept": True}})["ok"]
    assert m.contracts[1]["status"] == "open"
    r = answer_market(m, c, contract_reply={"contract": 1, "accept": "yes"})
    assert any("executed" in x for x in r["applied"]) and m.contracts[1]["status"] == "done"
    assert (a.coins, a.units["machine"] != [], b.units["grinder"] != [], c.coins) == (300 - 120 - 5, True, True,
                                                                                         20 + 120 - 5)
    assert b.coins == 15 and not b.units["machine"] and not c.units["grinder"]
    assert m.deals[-1]["kind"] == "contract" and m.deals[-1]["text"].startswith("Contract #1")


def test_contract_refusals_decline_expiry_and_failure(clock):
    m, a, b, c, terms = three(clock)
    bad = dict(terms, **{"Team 3": {"give": {"grinder": 1}, "get": {"coins": 100}}})
    r = answer_market(m, a, contract={"parties": bad})
    assert any("coins: parties give 120 but get 100" in d for d in r["dropped"])
    r = answer_market(m, b, contract={"parties": {k: v for k, v in terms.items() if k != "Team 2"}})
    assert any("you must be one of the parties" in d for d in r["dropped"])
    m.create_shell(a, "Extra Co")
    m.create_shell(b, "More Co")
    five = dict(terms, **{"Extra Co": {"give": {}, "get": {}}, "More Co": {"give": {}, "get": {}}})
    assert any("2 to 4 parties" in d for d in answer_market(m, a, contract={"parties": five})["dropped"])
    answer_market(m, a, contract={"parties": terms})
    answer_market(m, b, contract_reply={"contract": 1, "accept": False})
    assert m.contracts[1]["status"] == "declined"
    answer_market(m, a, contract={"parties": terms})
    clock.advance(61)
    m.tick()
    assert m.contracts[2]["status"] == "expired"
    answer_market(m, a, contract={"parties": terms})
    answer_market(m, b, contract_reply={"contract": 3, "accept": True})
    set_hand(m, c, 20)  # Team 3 lost its grinder before accepting
    r = answer_market(m, c, contract_reply={"contract": 3, "accept": True})
    assert m.contracts[3]["status"] == "failed" and any("cannot pay" in d for d in r["dropped"])
    assert a.coins == 300 and b.units["machine"]


# ---------------------------------------------------------------- restart in the middle of a version 2 game
def test_restore_keeps_version_2_state(tmp_path, clock):
    db = str(tmp_path / "v2.db")
    m = make_market(clock, n_teams=3, house=False, store=Store(db), game_minutes=5, last_call_minutes=4.5)
    m.start_game()
    a, b, c = ag(m, "Team 1"), ag(m, "Team 2"), ag(m, "Team 3")
    s = m.create_shell(a, "Bean Brothers")  # made after the game started
    item = m.round["orders_plan"][0]["item"]
    give_a = next(t for t in ITEMS if a.units[t])
    answer_market(m, a, transfer={"to": "Bean Brothers", "give": {"coins": 100, give_a: 1}})
    answer_market(m, b, audit=["Bean Brothers", "Team 1"])
    other = next(t for t in ITEMS if b.units[t])
    assert deal(m, b, c, {other: 1}, {"coins": 40})["deal"]
    at(m, clock, 90)
    seller = next((x for x in (a, b, c, s) if x.units[item]), None)
    if seller is not None:
        answer_market(m, seller, house_ask={"order": 1, "price": 5})
    at(m, clock, 151)
    cg = next(t for t in ITEMS if c.units[t])
    terms = {"Team 1": {"give": {"coins": 30}, "get": {}}, "Team 3": {"give": {cg: 1}, "get": {"coins": 30}},
             "Bean Brothers": {"give": {}, "get": {cg: 1}}}
    answer_market(m, a, contract={"parties": terms})
    answer_market(m, c, contract_reply={"contract": 1, "accept": True})
    answer_market(m, s, contract_reply={"contract": 1, "accept": True})
    assert m.contracts[1]["status"] == "done", [n for n in s.notices]
    at(m, clock, 140 + 60)  # two news items have landed (at 72 and 132 s)
    assert sum(n["done"] for n in m.round["news"]) >= 2
    before = {x.name: (x.coins, x.items()) for x in m.active_agents()}
    scores = {x.name: m.team_score(x.id) for x in m.teams()}
    mult, n_deals, n_private = dict(m.mult), len(m.deals), len(m.private)
    m.store.db.close()
    clock.advance(3)
    m2 = Market(Config(game_minutes=5, last_call_minutes=4.5), store=Store(db), clock=clock, secret=m.secret)
    assert m2.restore() is True
    assert {x.name: (x.coins, x.items()) for x in m2.active_agents()} == before
    assert m2.mult == mult and len(m2.deals) == n_deals and len(m2.private) == n_private
    assert {x.name: m2.team_score(x.id) for x in m2.teams()} == scores
    assert ag(m2, "Bean Brothers").owner == a.id and ag(m2, "Team 1").members
    assert all(d.get("kind") != "transfer" and d.get("kind") != "audit" for d in m2.deals)
    exp = m2.export()
    assert len(exp["v2"]["transfers"]) == 1 and len(exp["v2"]["audits"]) == 1
    assert all(d["kind"] in ("deal", "house_fill", "contract") for d in exp["deals"])
    assert m2.orders and m2.orders[0]["status"] != "open"


# ---------------------------------------------------------------- house bots in a version 2 world
def test_house_bots_play_version_2(clock):
    m = make_market(clock, n_teams=6, house=True, house_order_every_s=60, house_order_first_s=20,
                    house_order_window_s=30, practice_reset_minutes=15)
    brains = {a.id: Brain(["Fair Trader", "Hardliner", "Wildcard", "Opener"][i % 4], random.Random(i))
              for i, a in enumerate(m.agents.values())}
    rng = random.Random(4)
    bad = []
    t_end = clock() + 14 * 60
    while clock() < t_end:
        clock.advance(rng.uniform(0.3, 2.5))
        m.tick()
        for a in list(m.agents.values()):
            ev = m.pop_event(a.id)
            if ev and ev["type"] in ("market", "your_turn"):
                ans = brains[a.id].decide(ev)
                ans["event_id"] = ev["event_id"]
                res = m.act(a.id, ans)
                bad += [d for d in res["dropped"] if "unknown" in d or "not open" in d]
        assert all(x.coins >= 0 for x in m.agents.values())
    fills = [d for d in m.deals if d.get("kind") == "house_fill"]
    assert len(fills) >= 1 and len([d for d in m.deals if d.get("kind") == "deal"]) >= 10
    assert not bad, bad[:5]


# ---------------------------------------------------------------- the old starter bot still plays
def test_old_bot_still_plays_against_version_2(tmp_path):
    """Teams that keep their round 1 bot.py (from git, before version 2) can still play with a team key."""
    from sim_bots import FakeLLM
    from test_e2e import Server
    old = subprocess.run(["git", "-C", ROOT, "show", "2b73250:starter/bot.py"], capture_output=True, text=True)
    brief = subprocess.run(["git", "-C", ROOT, "show", "2b73250:starter/briefing.md"], capture_output=True, text=True)
    if old.returncode != 0:
        pytest.skip("git history not available")
    d = tmp_path / "oldbot"
    d.mkdir()
    (d / "bot.py").write_text(old.stdout)
    (d / "briefing.md").write_text(brief.stdout)
    srv = Server(tmp_path)
    srv.start()
    llm = FakeLLM(seed=3)
    try:
        import requests
        key = requests.post(srv.base + "/api/register", json={"team": "Old Bot", "members": ["Olga Oldbot"]},
                            timeout=10).json()["key"]
        env = {k: v for k, v in os.environ.items() if k not in ("LLM", "LLM_COMMAND", "ANTHROPIC_API_KEY", "MODEL")}
        env.update({"EXCHANGE_URL": srv.base, "TEAM_KEY": key, "OLLAMA_MODEL": "fake", "PYTHONUNBUFFERED": "1",
                    "OLLAMA_URL": f"http://127.0.0.1:{llm.port}"})
        log = open(tmp_path / "old.log", "w")
        p = subprocess.Popen([os.path.join(ROOT, ".venv", "bin", "python"), str(d / "bot.py")], env=env,
                             stdout=log, stderr=subprocess.STDOUT)
        time.sleep(45)
        p.terminate()
        p.wait(10)
        out = open(tmp_path / "old.log").read()
        answered = out.count("-> ")
        assert answered >= 3 and "Traceback" not in out and "Server says" not in out, out[-2000:]
    finally:
        llm.close()
        srv.kill()  # a long-poll still open would hold a graceful shutdown for 30 s
