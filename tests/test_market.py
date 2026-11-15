"""Engine tests with a fake clock: rules from the spec, one by one.

These run the CLASSIC rules (v2_rules=False, no member names): they prove the host's safety switch still plays the
original game. tests/test_v2.py covers the version 2 rules."""
import random
from collections import Counter

import pytest

from conftest import FakeClock
from conftest import make_market as _make_market
from server.config import Config
from server.house_bots import Brain
from server.items import ITEMS, normalize_offer
from server.market import GameError, Market
from server.store import Store


CLASSIC = {"v2_rules": False, "require_members": False}


def make_market(clock, **kw):
    for k, v in CLASSIC.items():
        kw.setdefault(k, v)
    return _make_market(clock, **kw)


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


def market_event(m, a):
    a.market_due_at = m.clock()
    ev = m.pop_event(a.id)
    assert ev and ev["type"] == "market", ev
    return ev


def open_chat(m, a, b, message="hi", offer=None):
    ev = market_event(m, a)
    res = m.act(a.id, {"event_id": ev["event_id"], "open": [{"to": b.name, "message": message, "offer": offer}]})
    assert any("opened chat" in x for x in res["applied"]), res
    return m.chats[m.pair_chat[(min(a.id, b.id), max(a.id, b.id))]]


def turn(m, a):
    a.market_due_at = None
    ev = m.pop_event(a.id)
    assert ev and ev["type"] == "your_turn", ev
    return ev


def total_coins(m):
    return sum(a.coins for a in m.active_agents())


def total_units(m):
    return Counter(t for a in m.active_agents() for t in ITEMS for _ in a.units[t])


# ---------------------------------------------------------------- dealing
@pytest.mark.parametrize("n", [5, 10, 13, 20])
def test_deal_is_fair_and_balanced(clock, n):
    m = make_market(clock, n_teams=n, house=False)
    agents = m.active_agents()
    assert len(agents) == n
    for a in agents:
        assert a.wealth() == 1500
        assert 500 <= a.coins <= 1450
        assert sum(a.items().values()) == 5
        assert sorted(a.card.values()) == [10, 30, 60, 100, 200]
    units = total_units(m)
    assert all(units[t] == n for t in ITEMS)  # one unit of each item per agent
    for t in ITEMS:  # Latin square: each item gets each value equally often (within 1)
        c = Counter(a.card[t] for a in agents)
        assert max(c.values()) - min(c.get(v, 0) for v in (10, 30, 60, 100, 200)) <= 1
    serials = [s for a in agents for t in ITEMS for s in a.units[t]]
    assert len(serials) == len(set(serials)) and all("-" in s for s in serials)


def test_mid_round_join_gets_a_fair_hand(clock):
    m = make_market(clock, n_teams=2)
    late = m.register("Late Team")
    assert late.active and late.wealth() == 1500 and sum(late.items().values()) == 5
    assert m.pop_event(late.id)["type"] == "market"


# ---------------------------------------------------------------- registration
def test_registration_rules(clock):
    m = make_market(clock, n_teams=1)
    with pytest.raises(GameError):
        m.register("Team 1")  # taken
    with pytest.raises(GameError):
        m.register("team 1")  # case-insensitive
    with pytest.raises(GameError):
        m.register("Wildcard")  # house name
    with pytest.raises(GameError):
        m.register("HOUSE")  # reserved
    with pytest.raises(GameError):
        m.register("x")  # too short
    assert m.register("  Bean   There!<script> ").name == "Bean There!script"
    a = m.register("Crema Cartel")
    assert m.name_from_key(a.key) == "Crema Cartel"
    assert m.name_from_key(a.key[:-1] + "0") is None
    m.start_game()
    with pytest.raises(GameError):
        m.register("Too Late")


def test_signed_key_survives_lost_disk(clock):
    m1 = make_market(clock, n_teams=0)
    key = m1.register("Crema Cartel").key
    m2 = Market(Config(**CLASSIC), store=Store(":memory:"), clock=clock, secret=m1.secret)
    m2.start_round("practice")
    a = m2.agent_for_key(key)
    assert a is not None and a.name == "Crema Cartel" and a.active
    assert m2.agent_for_key("esx_Zm9v.deadbeef") is None


# ---------------------------------------------------------------- offers
def test_offer_normalization():
    o, e = normalize_offer({"give": {"Burr Grinder": "1"}, "get": {"coin": 80}})
    assert o == {"give": {"grinder": 1}, "get": {"coins": 80}} and not e
    o, e = normalize_offer('{"give": {"gesha beans": 2}, "get": {"Espresso Machine": 1}}')
    assert o == {"give": {"beans": 2}, "get": {"machine": 1}}
    o, e = normalize_offer({"give": [{"item": "grinders", "qty": 1}], "get": {"coins": 5}})
    assert o == {"give": {"grinder": 1}, "get": {"coins": 5}}
    o, e = normalize_offer({"give": {"grinder": 1}, "get": {}})
    assert o is None and e
    o, e = normalize_offer({"give": {"unicorn": 1}, "get": {"coins": 5}})
    assert o is None
    o, e = normalize_offer({"give": {"grinder": -1}, "get": {"coins": 5}})
    assert o is None
    o, e = normalize_offer({"give": {"grinder": 1, "coins": 10}, "get": {"coins": 30}})
    assert o == {"give": {"grinder": 1}, "get": {"coins": 20}}  # netted


# ---------------------------------------------------------------- chats and deals
def test_full_negotiation_and_deal(clock):
    m = make_market(clock, n_teams=2, house=False)
    a, b = ag(m, "Team 1"), ag(m, "Team 2")
    set_hand(m, a, coins=1000, grinder=2)
    set_hand(m, b, coins=500, beans=1)
    coins0, units0 = total_coins(m), total_units(m)
    c = open_chat(m, a, b, "Word on the board is you want Grinders.")
    ev = turn(m, b)
    assert ev["partner"] == "Team 1" and ev["transcript"][0]["text"].startswith("Word on")
    assert ev["their_offer"] is None and ev["messages_left"] == 19 and ev["board"] == [] and "ledger" in ev
    r = m.act(b.id, {"event_id": ev["event_id"], "action": "offer", "message": "60 for one",
                     "offer": {"give": {"coins": 60}, "get": {"grinder": 1}}})
    assert r["ok"], r
    # replaying the same event is stale
    assert not m.act(b.id, {"event_id": ev["event_id"], "action": "talk", "message": "again"})["ok"]
    ev = turn(m, a)
    assert ev["their_offer"] == {"give": {"coins": 60}, "get": {"grinder": 1}}
    assert ev["if_you_accept"]["you_can_pay"] is True
    r = m.act(a.id, {"event_id": ev["event_id"], "action": "offer", "message": "110. Final.",
                     "offer": {"give": {"grinder": 1}, "get": {"coins": 110}}})
    ev = turn(m, b)
    assert ev["if_you_accept"]["wealth_change"] == b.card["grinder"] - 110
    r = m.act(b.id, {"event_id": ev["event_id"], "action": "accept", "message": "Deal."})
    assert r["ok"] and r["deal"], r
    assert c.status == "deal" and c.id in [d["chat_id"] for d in m.deals]
    assert a.coins == 1110 and len(a.units["grinder"]) == 1
    assert b.coins == 390 and len(b.units["grinder"]) == 1
    assert m.deals[-1]["text"] == "Team 1 sold 1 grinder to Team 2 for 110 coins"
    assert total_coins(m) == coins0 and total_units(m) == units0
    assert (min(a.id, b.id), max(a.id, b.id)) not in m.pair_chat  # may reopen
    assert any("Deal with Team 2" in n for n in a.notices)
    open_chat(m, b, a, "again?")


def test_strict_alternation_and_one_chat_per_pair(clock):
    m = make_market(clock, n_teams=2, house=False)
    a, b = ag(m, "Team 1"), ag(m, "Team 2")
    c = open_chat(m, a, b)
    # a cannot talk twice in a row
    r = m.act(a.id, {"event_id": f"t-{c.id}-{len(c.messages)}", "action": "talk", "message": "hello??"})
    assert not r["ok"] and "stale turn" in r["dropped"][0]
    # second chat with the same partner is refused while one is open
    ev = market_event(m, b)
    r = m.act(b.id, {"event_id": ev["event_id"], "open": [{"to": "Team 1", "message": "hi"}]})
    assert "already have an open chat" in r["dropped"][0]
    # and nobody can chat with themselves
    ev = market_event(m, a)
    r = m.act(a.id, {"event_id": ev["event_id"], "open": [{"to": "team 1", "message": "me"}]})
    assert "yourself" in r["dropped"][0]


def test_offer_you_cannot_pay_is_dropped_but_words_go_through(clock):
    m = make_market(clock, n_teams=2, house=False)
    a, b = ag(m, "Team 1"), ag(m, "Team 2")
    set_hand(m, a, coins=100, beans=1)
    c = open_chat(m, b, a)
    ev = turn(m, a)
    r = m.act(a.id, {"event_id": ev["event_id"], "action": "offer", "message": "2 grinders for 300",
                     "offer": {"give": {"grinder": 2}, "get": {"coins": 300}}})
    assert not r["ok"] and "do not hold 2 grinder" in r["dropped"][0]
    assert c.messages[-1]["action"] == "talk" and a.id not in c.offers
    assert c.whose_turn == b.id  # the words were still delivered
    assert any("do not hold" in n for n in a.notices)


def test_double_promise_second_accept_fails_and_chat_goes_on(clock):
    m = make_market(clock, n_teams=3, house=False)
    s, b1, b2 = ag(m, "Team 1"), ag(m, "Team 2"), ag(m, "Team 3")
    set_hand(m, s, coins=0, grinder=1)
    set_hand(m, b1, coins=500)
    set_hand(m, b2, coins=500)
    offer = {"give": {"grinder": 1}, "get": {"coins": 100}}
    c1 = open_chat(m, s, b1, "grinder for 100", offer)
    c2 = open_chat(m, s, b2, "grinder for 100", offer)
    e1, e2 = turn(m, b1), turn(m, b2)
    if e1["chat_id"] != c1.id:
        e1, e2 = e2, e1
    assert m.act(b1.id, {"event_id": e1["event_id"], "action": "accept"})["ok"]
    r = m.act(b2.id, {"event_id": e2["event_id"], "action": "accept"})
    assert not r["ok"] and "no longer holds 1 grinder" in r["dropped"][-1]
    assert c2.status == "open" and c2.whose_turn == s.id
    assert c2.messages[-1]["system"] and "Deal failed" in c2.messages[-1]["text"]
    assert s.id not in c2.offers  # unpayable offer withdrawn
    assert any("Deal failed" in n for n in s.notices)
    assert b2.coins == 500 and s.coins == 100


def test_acceptor_who_cannot_pay_fails(clock):
    m = make_market(clock, n_teams=2, house=False)
    s, b = ag(m, "Team 1"), ag(m, "Team 2")
    set_hand(m, s, coins=0, machine=1)
    set_hand(m, b, coins=50)
    open_chat(m, s, b, "machine for 300", {"give": {"machine": 1}, "get": {"coins": 300}})
    ev = turn(m, b)
    assert ev["if_you_accept"]["you_can_pay"] is False
    r = m.act(b.id, {"event_id": ev["event_id"], "action": "accept"})
    assert "cannot pay 250 coins" in r["dropped"][-1]


def test_accept_without_offer_and_talk_with_offer(clock):
    m = make_market(clock, n_teams=2, house=False)
    a, b = ag(m, "Team 1"), ag(m, "Team 2")
    c = open_chat(m, a, b)
    ev = turn(m, b)
    r = m.act(b.id, {"event_id": ev["event_id"], "action": "accept", "message": "ok"})
    assert "has no offer" in r["dropped"][0] and c.status == "open"
    ev = turn(m, a)
    r = m.act(a.id, {"event_id": ev["event_id"], "action": "talk", "message": "x",
                     "offer": {"give": {"coins": 1}, "get": {"beans": 1}}})
    assert "offer ignored" in r["dropped"][0] and a.id not in c.offers


def test_swap_items_for_items(clock):
    m = make_market(clock, n_teams=2, house=False)
    a, b = ag(m, "Team 1"), ag(m, "Team 2")
    set_hand(m, a, coins=100, grinder=1)
    set_hand(m, b, coins=100, beans=1)
    open_chat(m, a, b, "swap?", {"give": {"grinder": 1, "coins": 20}, "get": {"beans": 1}})
    ev = turn(m, b)
    assert m.act(b.id, {"event_id": ev["event_id"], "action": "accept"})["ok"]
    assert a.items()["beans"] == 1 and b.items()["grinder"] == 1 and a.coins == 80 and b.coins == 120
    assert "traded 1 grinder + 20 coins to Team 2 for 1 beans" in m.deals[-1]["text"]


def test_end_action_and_reopen(clock):
    m = make_market(clock, n_teams=2, house=False)
    a, b = ag(m, "Team 1"), ag(m, "Team 2")
    c = open_chat(m, a, b)
    ev = turn(m, b)
    m.act(b.id, {"event_id": ev["event_id"], "action": "walk away", "message": "bye"})
    assert c.status == "ended" and "Team 2 walked away" in c.end_reason and c.ended_by == b.id
    assert any("ended" in n for n in a.notices)
    open_chat(m, a, b, "second try")


def test_reply_timeout_ends_chat(clock):
    m = make_market(clock, n_teams=2, house=False)
    a, b = ag(m, "Team 1"), ag(m, "Team 2")
    c = open_chat(m, a, b)
    clock.advance(89)
    m.tick()
    assert c.status == "open"
    clock.advance(2)
    m.tick()
    assert c.status == "ended" and "Team 2 did not reply within 90 s" in c.end_reason


def test_message_limit(clock):
    m = make_market(clock, n_teams=2, house=False)
    a, b = ag(m, "Team 1"), ag(m, "Team 2")
    c = open_chat(m, a, b)
    who = b
    for i in range(19):
        ev = turn(m, who)
        if i == 0:
            assert ev["messages_left"] == 19
        m.act(who.id, {"event_id": ev["event_id"], "action": "talk", "message": f"m{i}"})
        who = a if who is b else b
    assert c.status == "ended" and "message limit (20)" in c.end_reason and c.n_msgs == 20


def test_long_messages_are_cut(clock):
    m = make_market(clock, n_teams=2, house=False)
    a, b = ag(m, "Team 1"), ag(m, "Team 2")
    c = open_chat(m, a, b, "x" * 500)
    assert len(c.messages[0]["text"]) == 300


# ---------------------------------------------------------------- board
def test_board_posts_rate_limit_and_cut(clock):
    m = make_market(clock, n_teams=1, house=False)
    a = ag(m, "Team 1")
    for i in range(3):
        ev = market_event(m, a)
        assert m.act(a.id, {"event_id": ev["event_id"], "post": f"post {i}"})["ok"]
        clock.advance(5)
    ev = market_event(m, a)
    r = m.act(a.id, {"event_id": ev["event_id"], "post": "fourth"})
    assert "up to 3 posts per minute" in r["dropped"][0]
    clock.advance(50)
    ev = market_event(m, a)
    r = m.act(a.id, {"event_id": ev["event_id"], "post": "y" * 400})
    assert "posted to the board" in r["applied"] and len(m.posts[-1]["text"]) == 200
    assert m.posts[-1]["author"] == "Team 1"
    b = m.board(since=m.posts[1]["id"])
    assert [p["text"] for p in b["posts"]][0] == "post 2" and b["next_since"] == m.posts[-1]["id"]


def test_posts_allowed_in_turn_answers_and_board_reaches_events(clock):
    m = make_market(clock, n_teams=2, house=False)
    a, b = ag(m, "Team 1"), ag(m, "Team 2")
    open_chat(m, a, b)
    ev = turn(m, b)
    m.act(b.id, {"event_id": ev["event_id"], "action": "talk", "message": "hm", "post": "Beans are crashing!"})
    ev = turn(m, a)
    assert ev["board"][-1]["text"] == "Beans are crashing!" and ev["board"][-1]["author"] == "Team 2"
    assert market_event(m, a)["board"][-1]["text"] == "Beans are crashing!"


def test_host_notice_reaches_everyone(clock):
    m = make_market(clock, n_teams=2, house=False)
    m.post_house_notice("Chats close in 5 minutes")
    assert m.posts[-1]["author"] == "HOUSE"
    for a in m.active_agents():
        assert any("HOUSE NOTICE" in n for n in a.notices)
        assert market_event(m, a)["house_notice"] == "Chats close in 5 minutes"


# ---------------------------------------------------------------- event delivery
def test_market_updates_every_30_seconds_and_turn_redelivery(clock):
    m = make_market(clock, n_teams=2, house=False)
    a, b = ag(m, "Team 1"), ag(m, "Team 2")
    assert m.pop_event(a.id)["type"] == "market"
    assert m.pop_event(a.id) is None
    clock.advance(30)
    m.tick()
    assert m.pop_event(a.id)["type"] == "market"
    c = open_chat(m, a, b)
    ev = turn(m, b)
    nxt = m.pop_event(b.id)
    assert nxt is None or nxt["type"] == "market"  # the delivered turn is not handed out twice
    clock.advance(46)  # bot crashed without answering: the turn comes back
    b.market_due_at = None
    ev2 = m.pop_event(b.id)
    assert ev2["type"] == "your_turn" and ev2["event_id"] == ev["event_id"] and ev2["chat_id"] == c.id


def test_oldest_ready_event_first(clock):
    m = make_market(clock, n_teams=3, house=False)
    a, b, c = ag(m, "Team 1"), ag(m, "Team 2"), ag(m, "Team 3")
    for x in (a, b, c):
        m.pop_event(x.id)
    open_chat(m, b, a)
    clock.advance(1)
    open_chat(m, c, a)
    clock.advance(1)
    a.market_due_at = clock()
    e1, e2, e3 = m.pop_event(a.id), m.pop_event(a.id), m.pop_event(a.id)
    assert [e1["partner"], e2["partner"], e3["type"]] == ["Team 2", "Team 3", "market"]


def test_garbage_answers_never_crash(clock):
    m = make_market(clock, n_teams=2, house=False)
    a, b = ag(m, "Team 1"), ag(m, "Team 2")
    for junk in (None, [], "hello", 42, {"event_id": None}, {"event_id": "t-999-0"}, {"event_id": "m-0-0-0"},
                 {"event_id": "zzz", "action": "accept"}):
        r = m.act(a.id, junk)
        assert r["ok"] is False
    ev = market_event(m, a)
    r = m.act(a.id, {"event_id": ev["event_id"], "open": [None, 5, {"to": None}, {"to": "Nobody"},
                                                          {"to": "Team 2", "offer": "nonsense"}]})
    assert "opened chat" in " ".join(r["applied"]) and len(r["dropped"]) >= 4
    ev = turn(m, b)
    r = m.act(b.id, {"event_id": ev["event_id"], "action": "dance", "message": {"nested": True}})
    assert "unknown action" in r["dropped"][0]


# ---------------------------------------------------------------- practice and game
def test_practice_resets_every_10_minutes_and_records_gains(clock):
    m = make_market(clock, n_teams=2)
    r1 = m.round["id"]
    clock.advance(10 * 60 + 1)
    m.tick()
    assert m.round["id"] == r1 + 1 and m.mode == "practice"
    h = m.practice_history[-1]
    assert h["round"] == r1 and set(h["gains"]) >= {"Team 1", "Team 2", "Fair Trader"}
    assert all(a.wealth() == 1500 for a in m.active_agents())


def test_game_clock_last_call_bell_and_reveal(clock):
    m = make_market(clock, n_teams=4)
    a, b = ag(m, "Team 1"), ag(m, "Team 2")
    m.start_game()
    assert m.mode == "game" and not m.registration_open
    assert all(not x.active for x in m.agents.values() if x.is_house)  # house bots sit out the game
    assert len(m.active_agents()) == 4
    clock.advance(22 * 60)
    m.tick()
    assert m.round["last_call_done"] and m.posts[-1]["author"] == "HOUSE" and "Last call" in m.posts[-1]["text"]
    clock.advance(3 * 60 - 30)
    m.tick()
    c = open_chat(m, a, b)
    clock.advance(30)
    m.tick()
    assert m.mode == "over" and c.status == "ended" and c.end_reason == "the bell rang"
    ev = m.pop_event(a.id)
    assert ev["type"] == "game_over" and ev["rank"] >= 1 and len(ev["standings"]) == 4
    assert m.wait_event(a.id)["type"] == "game_over"  # a restarted bot still learns the result
    r = m.act(a.id, {"event_id": "m-1", "post": "too late"})
    assert "market is closed" in r["dropped"][0]
    st = m.final["standings"]
    assert [s["rank"] for s in st] == [1, 2, 3, 4]
    assert all(st[i]["wealth"] >= st[i + 1]["wealth"] for i in range(3))
    assert "card" in st[0] and "fun_facts" in m.final
    d = m.dashboard()
    assert "card" not in str(d["final"]) and m.reveal() and "card" in str(m.dashboard()["final"])
    m.to_practice()
    assert m.mode == "practice" and m.registration_open and len(m.active_agents()) == 8


def test_tie_break_more_coins_then_coin_flip(clock):
    m = make_market(clock, n_teams=3, house=False)
    m.start_game()
    a, b, c = ag(m, "Team 1"), ag(m, "Team 2"), ag(m, "Team 3")
    for x in (a, b, c):
        set_hand(m, x, coins=1000)
    low = min(a.card, key=a.card.get)  # Team 1: same Wealth, fewer coins
    set_hand(m, a, coins=1000 - a.card[low], **{low: 1})
    assert a.wealth() == b.wealth() == c.wealth() == 1000
    m.ring_bell()
    st = {s["name"]: s for s in m.final["standings"]}
    assert st["Team 1"]["rank"] == 3  # tied on Wealth, fewest coins
    assert {st["Team 2"]["rank"], st["Team 3"]["rank"]} == {1, 2}
    assert st["Team 2"]["coin_flip"] and st["Team 3"]["coin_flip"] and not st["Team 1"]["coin_flip"]


def test_house_bots_join_the_game_when_the_host_says_so(clock):
    m = make_market(clock, n_teams=2, house_in_game=True)
    m.start_game()
    assert len(m.active_agents()) == 6


# ---------------------------------------------------------------- views and privacy
def test_dashboard_has_wealth_but_never_chat_words(clock):
    m = make_market(clock, n_teams=2, house=False)
    a, b = ag(m, "Team 1"), ag(m, "Team 2")
    open_chat(m, a, b, "SECRET-WORDS here", {"give": {"coins": 7}, "get": {"beans": 1}})
    d = m.dashboard()
    assert "SECRET-WORDS" not in str(d) and {x["name"] for x in d["agents"]} == {"Team 1", "Team 2"}
    assert d["chats"][0]["a"] == "Team 1" and d["chats"][0]["status"] == "open"
    assert "SECRET-WORDS" in str(m.watch_chats()) and "SECRET-WORDS" in str(m.watch_chat(d["chats"][0]["id"]))
    assert market_event(m, a)["dashboard_wealth"] == {"Team 1": a.wealth(), "Team 2": b.wealth()}


def test_team_log_shows_only_own_chats(clock):
    m = make_market(clock, n_teams=3, house=False)
    a, b, c = ag(m, "Team 1"), ag(m, "Team 2"), ag(m, "Team 3")
    open_chat(m, a, b, "a-b words")
    open_chat(m, b, c, "b-c words")
    log = m.team_log(a)
    assert "a-b words" in str(log) and "b-c words" not in str(log)


# ---------------------------------------------------------------- simulation invariants
def run_sim(m, clock, minutes, seed=1):
    rng = random.Random(seed)
    brains = {a.id: Brain(["Fair Trader", "Hardliner", "Wildcard", "Opener"][i % 4], random.Random(seed + i))
              for i, a in enumerate(m.agents.values())}
    t_end = clock() + minutes * 60
    while clock() < t_end:
        clock.advance(rng.uniform(0.3, 2.5))
        m.tick()
        for a in list(m.agents.values()):
            ev = m.pop_event(a.id)
            if ev and ev["type"] in ("market", "your_turn"):
                ans = brains[a.id].decide(ev)
                ans["event_id"] = ev["event_id"]
                m.act(a.id, ans)
            if any(x.coins < 0 or any(len(x.units[t]) < 0 for t in ITEMS) for x in m.agents.values()):
                raise AssertionError("negative holdings")


def check_invariants(m, coins0, units0):
    assert total_coins(m) == coins0
    assert total_units(m) == units0
    for serial, u in m.units.items():
        owner = m.agents[u["owner"]]
        assert serial in owner.units[u["type"]]
    for c in m.chats.values():
        authors = [x["author"] for x in c.messages if not x.get("system")]
        assert c.n_msgs <= 20
        # strict alternation, except a failed accept which passes the turn
        for i in range(1, len(c.messages)):
            prev, cur = c.messages[i - 1], c.messages[i]
            if not prev.get("system") and not cur.get("system"):
                assert prev["author"] != cur["author"], (c.id, authors)


def test_simulated_practice_round_keeps_books_balanced(clock):
    m = make_market(clock, n_teams=12, house=True)
    coins0, units0 = total_coins(m), total_units(m)
    run_sim(m, clock, minutes=8)
    assert len(m.deals) > 10, "the house brains should trade"
    check_invariants(m, coins0, units0)
    for a in m.active_agents():
        times = list(a.post_times)
        assert all(times[i + 3] - times[i] >= 60 for i in range(len(times) - 3))


def test_simulated_game_to_the_bell(clock):
    m = make_market(clock, n_teams=16, house=True, house_in_game=True)
    m.start_game()
    coins0, units0 = total_coins(m), total_units(m)
    run_sim(m, clock, minutes=26, seed=3)
    assert m.mode == "over" and m.final and len(m.final["standings"]) == 20
    check_invariants(m, coins0, units0)
    f = m.final["fun_facts"]
    assert "best_deal" in f and "most_chats" in f
    # neutral wording: no fact calls a bot a liar; the board fact is the post that paid off most
    assert not any("lie" in k for k in f)
    if "best_board_post" in f:
        assert f["best_board_post"]["gain"] > 0 and set(f["best_board_post"]) == {"agent", "post", "gain"}
    text = m.transcripts_text()
    assert "## Final standings" in text and "## Chats" in text


# ---------------------------------------------------------------- persistence
def test_restart_mid_game_restores_holdings(tmp_path, clock):
    db = str(tmp_path / "x.db")
    m = make_market(clock, n_teams=6, house=True, store=Store(db))
    m.start_game()
    run_sim(m, clock, minutes=4, seed=5)
    assert m.deals
    before = {a.name: (a.coins, a.items(), a.card, a.wealth()) for a in m.active_agents()}
    posts = len(m.posts)
    m.store.db.close()
    clock.advance(5)
    m2 = Market(Config(**CLASSIC), store=Store(db), clock=clock, secret=m.secret)
    assert m2.restore() is True
    after = {a.name: (a.coins, a.items(), a.card, a.wealth()) for a in m2.active_agents()}
    assert after == before
    assert m2.mode == "game" and not m2.registration_open and len(m2.posts) == posts
    assert len(m2.deals) == len(m.deals) and not m2.open_chat_ids
    # keys still work
    a = m2.agent_for_key(next(x.key for x in m.agents.values() if not x.is_house))
    assert a is not None and a.active


def test_turn_event_explains_the_offer_in_plain_words(clock):
    m = make_market(clock, n_teams=2, house=False)
    a, b = ag(m, "Team 1"), ag(m, "Team 2")
    set_hand(m, a, coins=500)
    set_hand(m, b, coins=0, grinder=1)
    open_chat(m, a, b, "buying", {"give": {"coins": 130}, "get": {"grinder": 1}})
    ev = turn(m, b)
    s = ev["if_you_accept"]["summary"]
    assert s.startswith("Team 1 offers to give you 130 coins and wants 1 grinder from you.")
    assert f"{130 - b.card['grinder']:+d}" in s


def test_action_post_goes_to_the_board(clock):
    m = make_market(clock, n_teams=2, house=False)
    a, b = ag(m, "Team 1"), ag(m, "Team 2")
    open_chat(m, a, b)
    ev = turn(m, b)
    r = m.act(b.id, {"event_id": ev["event_id"], "action": "post", "message": "Beans are scarce!"})
    assert "posted to the board" in r["applied"] and m.posts[-1]["text"] == "Beans are scarce!"


def test_wipe_removes_teams_and_invalidates_keys(tmp_path, clock):
    m = make_market(clock, n_teams=3, store=Store(str(tmp_path / "w.db")))
    old_key = ag(m, "Team 1").key
    assert m.wipe_teams() == 3
    assert {a.name for a in m.active_agents()} == {"Fair Trader", "Hardliner", "Wildcard", "Opener"}
    assert m.agent_for_key(old_key) is None  # old signed keys no longer re-register
    new = m.register("Team 1")  # the name is free again
    assert new.key != old_key and m.agent_for_key(new.key) is new
    m2 = Market(Config(**CLASSIC), store=Store(str(tmp_path / "w.db")), clock=clock, secret=m.secret)
    m2.restore()
    assert m2.agent_for_key(new.key) is not None and m2.agent_for_key(old_key) is None


def test_backlogged_bot_gets_answerable_turns_first(clock):
    m = make_market(clock, n_teams=3, house=False)
    a, b, c = ag(m, "Team 1"), ag(m, "Team 2"), ag(m, "Team 3")
    for x in (a, b, c):
        m.pop_event(x.id)
    open_chat(m, b, a)          # older: will have only 10 s left
    clock.advance(80)
    open_chat(m, c, a)          # newer: 90 s left
    a.market_due_at = None
    first = m.pop_event(a.id)
    assert first["partner"] == "Team 3" and first["reply_within_s"] >= 15
    second = m.pop_event(a.id)
    assert second["partner"] == "Team 2"  # still delivered when nothing better waits


def test_wrapped_text_is_unwrapped(clock):
    m = make_market(clock, n_teams=2, house=False)
    a = ag(m, "Team 1")
    ev = market_event(m, a)
    m.act(a.id, {"event_id": ev["event_id"], "post": {"text": "Beans are rare", "ago_s": 0}})
    assert m.posts[-1]["text"] == "Beans are rare"


# ---------------------------------------------------------------- the questions a host asks first
def test_one_agent_talks_with_several_agents_at_the_same_time(clock):
    """A bot can run many 1-to-1 chats at once; each chat goes on by itself and can end in its own deal."""
    m = make_market(clock, n_teams=4, house=False)
    a, b, c, d = (ag(m, f"Team {i}") for i in range(1, 5))
    set_hand(m, a, coins=1000, grinder=1, beans=1)
    for x in (b, c, d):
        set_hand(m, x, coins=500)
    ev = market_event(m, a)
    r = m.act(a.id, {"event_id": ev["event_id"], "open": [
        {"to": "Team 2", "message": "grinder for 100", "offer": {"give": {"grinder": 1}, "get": {"coins": 100}}},
        {"to": "Team 3", "message": "beans for 80", "offer": {"give": {"beans": 1}, "get": {"coins": 80}}},
        {"to": "Team 4", "message": "just saying hi"}]})
    assert sum("opened chat" in x for x in r["applied"]) == 3, r
    chats = {x.id: m.chats[m.pair_chat[(min(a.id, x.id), max(a.id, x.id))]] for x in (b, c, d)}
    assert len(a.open_chats) == 3 and all(ch.status == "open" for ch in chats.values())
    assert {x["name"]: x for x in m.dashboard()["agents"]}["Team 1"]["open_chats"] == 3
    # all three partners get their turn; each answer lands in its own chat
    eb, ec, ed = turn(m, b), turn(m, c), turn(m, d)
    assert [eb["chat_id"], ec["chat_id"], ed["chat_id"]] == [chats[b.id].id, chats[c.id].id, chats[d.id].id]
    assert m.act(d.id, {"event_id": ed["event_id"], "action": "talk", "message": "hi back"})["ok"]
    assert m.act(b.id, {"event_id": eb["event_id"], "action": "accept"})["ok"]  # deal 1
    assert m.act(c.id, {"event_id": ec["event_id"], "action": "accept"})["ok"]  # deal 2, same minute
    assert (chats[b.id].status, chats[c.id].status, chats[d.id].status) == ("deal", "deal", "open")
    assert (a.coins, b.coins, c.coins) == (1180, 400, 420)
    assert a.items()["grinder"] == 0 and b.items()["grinder"] == 1 and c.items()["beans"] == 1
    # the third chat is still going: it is Team 1's turn there
    ev = turn(m, a)
    assert ev["chat_id"] == chats[d.id].id and ev["partner"] == "Team 4"


def test_deal_settles_at_once_and_the_sold_item_is_gone(clock):
    """Accept = coins and the item change hands in the same instant. The seller cannot sell that unit
    again anywhere; the buyer owns it and can sell it on."""
    m = make_market(clock, n_teams=4, house=False)
    s, b, c, d = (ag(m, f"Team {i}") for i in range(1, 5))
    set_hand(m, s, coins=200, machine=1)
    for x in (b, c, d):
        set_hand(m, x, coins=600)
    coins0, units0 = total_coins(m), total_units(m)
    serial = s.units["machine"][0]
    w_s, w_b = s.wealth(), b.wealth()
    open_chat(m, s, b, "machine for 250", {"give": {"machine": 1}, "get": {"coins": 250}})
    open_chat(m, s, c, "hello")
    ev = turn(m, b)
    assert m.act(b.id, {"event_id": ev["event_id"], "action": "accept", "message": "deal"})["ok"]
    # settled right away: coins, the unit's owner, Wealth, ledger and dashboard
    assert (s.coins, b.coins) == (450, 350)
    assert m.units[serial]["owner"] == b.id and serial in b.units["machine"] and serial not in s.units["machine"]
    assert s.wealth() == w_s - s.card["machine"] + 250 and b.wealth() == w_b + b.card["machine"] - 250
    assert m.deals[-1]["text"] == "Team 1 sold 1 machine to Team 2 for 250 coins"
    dash = {x["name"]: x for x in m.dashboard()["agents"]}
    assert dash["Team 1"]["wealth"] == s.wealth() and dash["Team 1"]["deals"] == dash["Team 2"]["deals"] == 1
    # the seller can no longer offer that machine, in any other chat
    ev = turn(m, c)
    assert m.act(c.id, {"event_id": ev["event_id"], "action": "talk", "message": "got a machine?"})["ok"]
    ev = turn(m, s)
    r = m.act(s.id, {"event_id": ev["event_id"], "action": "offer", "message": "machine for 300",
                     "offer": {"give": {"machine": 1}, "get": {"coins": 300}}})
    assert not r["ok"] and "do not hold 1 machine" in r["dropped"][0]
    # the buyer owns it now and sells it on
    open_chat(m, b, d, "machine for 300", {"give": {"machine": 1}, "get": {"coins": 300}})
    ev = turn(m, d)
    assert m.act(d.id, {"event_id": ev["event_id"], "action": "accept"})["ok"]
    assert m.units[serial]["owner"] == d.id
    assert [h[0] for h in m.units[serial]["history"]] == [s.id, b.id, d.id]
    assert total_coins(m) == coins0 and total_units(m) == units0
