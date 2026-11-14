"""HTTP API tests through FastAPI's TestClient (house bots off for determinism)."""
import importlib.util
import io
import json
import os
import sys
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

from server.app import create_app
from server.config import Config

HOST = {"X-Host-Key": "test-host"}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("HOST_KEY", "test-host")
    app = create_app(db_path=":memory:", house=False)
    with TestClient(app) as c:
        yield c


def reg(c, name):
    r = c.post("/api/register", json={"team": name})
    assert r.status_code == 200, r.text
    return {"X-Team-Key": r.json()["key"]}


def test_register_and_first_event(client):
    h = reg(client, "Crema Cartel")
    assert client.post("/api/register", json={"team": "crema cartel"}).status_code == 400
    assert client.post("/api/register", json={"team": "Opener"}).status_code == 400
    assert client.post("/api/register", json={}).status_code == 400
    assert client.post("/api/register", content=b"garbage").status_code == 400
    assert client.post("/api/register", json={"name": "By Name"}).status_code == 200  # 'name' works too
    ev = client.get("/api/next?wait=0", headers=h).json()
    assert ev["type"] == "market" and ev["you"] == "Crema Cartel" and ev["wealth"] == 1500
    assert "Opener" in ev["agents"] and "By Name" in ev["agents"]
    assert set(ev["dashboard_wealth"]) >= {"Crema Cartel", "Opener"}
    w = client.get("/api/next?wait=0", headers=h).json()
    assert w["type"] == "wait"


def test_bad_key_is_slow_and_explains(client):
    t0 = time.time()
    r = client.get("/api/next?wait=0", headers={"X-Team-Key": "nope"})
    assert r.status_code == 401 and r.json()["type"] == "error" and "register" in r.json()["error"]
    assert time.time() - t0 >= 1.9


def test_full_trade_over_http(client):
    a, b = reg(client, "Alpha"), reg(client, "Beta")
    ea = client.get("/api/next?wait=0", headers=a).json()
    client.get("/api/next?wait=0", headers=b)
    item = next(t for t, n in ea["holdings"]["items"].items() if n)
    r = client.post("/api/act", headers=a, json={"event_id": ea["event_id"], "post": "Selling!",
                                                 "open": [{"to": "Beta", "message": "Want this?",
                                                           "offer": {"give": {item: 1}, "get": {"coins": 1}}}]}).json()
    assert r["ok"], r
    eb = client.get("/api/next?wait=2", headers=b).json()
    assert eb["type"] == "your_turn" and eb["their_offer"]["give"] == {item: 1}
    assert eb["board"][-1]["text"] == "Selling!"
    r = client.post("/api/act", headers=b, json={"event_id": eb["event_id"], "action": "accept", "message": "Deal"}).json()
    assert r["ok"] and r["deal"]
    led = client.get("/api/dashboard").json()["ledger"]
    assert led[0]["text"] == f"Alpha sold 1 {item} to Beta for 1 coins"
    log = client.get("/api/log", headers=a).json()
    assert log["team"] == "Alpha" and log["deals"] and log["chats"][0]["status"] == "deal"
    assert client.get("/api/board").json()["posts"][-1]["text"] == "Selling!"
    assert client.get(f"/api/board?since={client.get('/api/board').json()['next_since']}").json()["posts"] == []


def test_act_with_broken_bodies(client):
    a = reg(client, "Alpha")
    for body in (b"not json", b"[]", b"null", b'"str"'):
        r = client.post("/api/act", headers={**a, "content-type": "application/json"}, content=body)
        assert r.status_code == 200 and r.json()["ok"] is False


def screen_key(c):
    return c.get("/api/host/state", headers=HOST).json()["screen_key"]


def test_chat_words_only_on_the_hosts_screen(client):
    """Red-team finding C1: during play nobody reads other teams' chats, except the host's screen."""
    a, b = reg(client, "Alpha"), reg(client, "Beta")
    ea = client.get("/api/next?wait=0", headers=a).json()
    client.post("/api/act", headers=a, json={"event_id": ea["event_id"], "open": [{"to": "Beta", "message": "psst SECRET"}]})
    d = client.get("/api/dashboard").json()
    assert "SECRET" not in json.dumps(d) and d["chats"][0]["status"] == "open"  # who chats with whom is public
    cid = d["chats"][0]["id"]
    # no key, a team key (header or ?key=), or a wrong screen key: no words
    for url in ("/api/watch/chats", f"/api/watch/chat/{cid}"):
        assert client.get(url).status_code == 403 and "SECRET" not in client.get(url).text
        assert client.get(url, headers=b).status_code == 403
        assert client.get(url + ("&" if "?" in url else "?") + "key=" + b["X-Team-Key"]).status_code == 403
    assert client.get("/api/watch/chats", headers={"X-Screen-Key": "guess"}).status_code == 403
    # a team key next to the right screen key is still refused: agents read only their own chats
    sk = {"X-Screen-Key": screen_key(client)}
    assert client.get("/api/watch/chats", headers={**sk, **b}).status_code == 403
    # the host's screen (screen key) and the host read everything
    assert "SECRET" in client.get("/api/watch/chats", headers=sk).text
    assert "SECRET" in client.get(f"/api/watch/chat/{cid}", headers=sk).text
    assert "SECRET" in client.get("/api/watch/chats", headers=HOST).text
    assert client.get("/api/watch/chat/999999", headers=sk).status_code == 404
    # the host can make chats public
    client.post("/api/host/config", headers=HOST, json={"public_chats": True})
    assert "SECRET" in client.get("/api/watch/chats").text
    client.post("/api/host/config", headers=HOST, json={"public_chats": False})
    assert client.get("/api/watch/chats").status_code == 403


def test_attack_findings_are_closed(monkeypatch):
    """Red-team findings H2, H3, M4, M5, M6, M7, each tried again."""
    monkeypatch.setenv("HOST_KEY", "test-host")
    monkeypatch.setenv("PROXY_HOPS", "1")
    with TestClient(create_app(db_path=":memory:", house=False)) as c:
        # H2: a forged X-Forwarded-For entry sits left of the proxy's; the proxy's entry counts
        st = c.get("/api/host/state", headers={**HOST, "X-Forwarded-For": "6.6.6.6, 10.1.2.3"}).json()
        assert st["your_address"] == "10.1.2.3"
        a = {"X-Team-Key": c.post("/api/register", json={"team": "Real Team"}, headers={"X-Forwarded-For": "10.7.7.7"}).json()["key"]}
        codes = [c.post("/api/register", json={"team": f"Spam {i}"},
                        headers={"X-Forwarded-For": f"9.9.{i}.9, 10.1.2.3"}).status_code for i in range(45)]
        assert codes[:38].count(200) == 38 and codes[-1] in (400, 429), codes  # limited, or the 40-team cap
        assert sum(x == 200 for x in codes) <= 40
        # M5: absurd numbers are refused in words, never a 500
        ev = c.get("/api/next?wait=0", headers=a).json()
        raw = '{"event_id": "%s", "open": [{"to": "Spam 1", "offer": {"give": {"coins": 1e400}, "get": {"beans": 1}}}]}' % ev["event_id"]
        r = c.post("/api/act", headers={**a, "content-type": "application/json"}, content=raw)
        assert r.status_code == 200 and "must be a number" in r.text
        # M6: bodies over 64 KB are refused before anyone reads them
        r = c.post("/api/act", headers={**a, "content-type": "application/json"}, content="x" * 70_000)
        assert r.status_code == 413
        # M7: wrong host keys are slow, then rate limited
        t = time.time()
        assert c.get("/api/host/state", headers={"X-Host-Key": "nope"}).status_code == 403
        assert time.time() - t >= 0.9
    # M4: look-alike letters cannot copy a house bot or the host
    with TestClient(create_app(db_path=":memory:", house=False)) as c:
        assert c.post("/api/register", json={"team": "Ｆａｉｒ Ｔｒａｄｅｒ"}).status_code == 400  # fullwidth = Fair Trader
        assert c.post("/api/register", json={"team": "ＨＯＳＴ"}).status_code == 400
        assert c.post("/api/register", json={"team": "Fa\u0456r Trader"}).json()["team"] == "Far Trader"  # Cyrillic i dropped
    # H3: every page escapes quotes, so text cannot break out of an HTML attribute
    static = os.path.join(os.path.dirname(__file__), "..", "server", "static")
    for f in ("index", "screen", "host", "scoreboard", "team"):
        page = open(os.path.join(static, f + ".html")).read()
        assert "'\"':'&quot;'" in page and "&#39;" in page, f


def test_host_controls(client):
    a = reg(client, "Alpha")
    reg(client, "Beta")
    assert client.get("/api/host/state").status_code == 403
    assert client.post("/api/host/start_game", headers={"X-Host-Key": "wrong"}).status_code == 403
    st = client.get("/api/host/state", headers=HOST).json()
    assert st["mode"] == "practice" and any(t["name"] == "Alpha" and t["key"] for t in st["teams"])
    r = client.post("/api/host/config", headers=HOST, json={"game_minutes": 0}).json()
    assert "between" in r["error"]
    assert client.post("/api/host/config", headers=HOST, json={"game_minutes": 3, "last_call_minutes": 2}).status_code == 200
    assert client.post("/api/host/notice", headers=HOST, json={"text": "Five minutes to freeze"}).status_code == 200
    assert client.get("/api/board").json()["posts"][-1]["author"] == "HOUSE"
    assert client.post("/api/host/reveal", headers=HOST).status_code == 400  # not before the bell
    st = client.post("/api/host/start_game", headers=HOST).json()
    assert st["mode"] == "game" and not st["registration_open"]
    assert client.post("/api/register", json={"team": "Late"}).status_code == 400
    assert client.get("/api/transcripts").status_code == 403
    ev = client.get("/api/next?wait=0", headers=a).json()
    assert ev["type"] == "market" and ev["seconds_left"] > 170
    client.post("/api/host/extend", headers=HOST, json={"minutes": 1})
    assert client.get("/api/next?wait=0", headers=a).json()["type"] == "wait"
    assert client.post("/api/host/bell", headers=HOST).json()["mode"] == "over"
    go = client.get("/api/next?wait=0", headers=a).json()
    assert go["type"] == "game_over" and go["of"] == 2
    assert client.post("/api/host/reveal", headers=HOST).json()["mode"] == "reveal"
    d = client.get("/api/dashboard").json()
    assert d["final"]["standings"][0]["card"] and "fun_facts" in d["final"]
    t = client.get("/api/transcripts")
    assert t.status_code == 200 and "Final standings" in t.text
    assert client.get("/api/transcripts?fmt=json").json()["final"]
    exp = client.get("/api/host/export", headers=HOST).json()
    assert exp["rounds"] and exp["agents"]
    assert client.post("/api/host/practice", headers=HOST).json()["mode"] == "practice"
    assert client.post("/api/host/remove", headers=HOST, json={"team": "Beta"}).status_code == 200
    assert client.get("/api/next?wait=0", headers={"X-Team-Key": "x"}).status_code == 401


def test_rate_limit(client):
    a = reg(client, "Alpha")
    client.post("/api/host/config", headers=HOST, json={"rate_limit_per_s": 2})
    codes = [client.get("/api/log", headers=a).status_code for _ in range(8)]
    assert 429 in codes and codes[0] == 200
    r = client.get("/api/log", headers=a)
    assert r.status_code in (200, 429)


def test_starter_kit_is_personalised(client):
    t = client.get("/starter/team-prompt.md?team=Crema%20Cartel").text
    assert "http://testserver" in t and "Crema Cartel" in t and "<URL>" not in t
    z = zipfile.ZipFile(io.BytesIO(client.get("/starter/espresso-starter.zip?team=X").content))
    assert sorted(z.namelist()) == ["espresso-starter/bot.py", "espresso-starter/briefing.md",
                                    "espresso-starter/strategy-guide.md", "espresso-starter/team-prompt.md"]
    assert "GET /api/board" in z.read("espresso-starter/briefing.md").decode()
    assert client.get("/starter/../server/app.py").status_code == 404
    assert client.get("/static/../app.py").status_code == 404


def test_pages_load(client):
    for p in ("/", "/screen", "/host", "/team", "/scoreboard", "/healthz", "/api/status", "/api/scoreboard"):
        assert client.get(p).status_code == 200, p


def test_starter_bot_decide_parses_chatty_llm_output(monkeypatch):
    monkeypatch.setenv("EXCHANGE_URL", "http://unused")
    monkeypatch.setenv("TEAM_KEY", "k")
    path = os.path.join(os.path.dirname(__file__), "..", "starter", "bot.py")
    spec = importlib.util.spec_from_file_location("starter_bot", path)
    bot = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bot)
    seen = {}

    def fake(system, user):
        seen["system"], seen["user"] = system, user
        return 'Sure! {"message": "60?", "action": "offer", "offer": {"give": {"coins": 60}, "get": {"grinder": 1}}, "post": null} hope that helps'

    monkeypatch.setattr(bot, "ask_llm", fake)
    ans = bot.decide({"type": "your_turn", "event_id": "t-1-1"})
    assert ans["action"] == "offer" and ans["offer"]["get"] == {"grinder": 1}
    assert "Agent Briefing" in seen["system"] and "# Your strategy" in seen["system"]
    assert '"your_turn"' in seen["user"] and "Answer with JSON only" in seen["user"]
    # seen with real Claude bots: a second JSON object after the answer; the first one counts
    monkeypatch.setattr(bot, "ask_llm", lambda s, u: '{"message": "ok", "action": "accept"}\n\n{"note": "my reasoning"}')
    assert bot.decide({"type": "your_turn", "event_id": "t-1-2"}) == {"message": "ok", "action": "accept"}


def test_host_wipe_and_defaults(client):
    a = reg(client, "Alpha")
    assert client.post("/api/host/wipe", headers=HOST, json={}).status_code == 400
    st = client.post("/api/host/wipe", headers=HOST, json={"confirm": "WIPE"}).json()
    assert not any(t["name"] == "Alpha" for t in st["teams"])
    assert client.get("/api/log", headers=a).status_code == 401
    client.post("/api/host/config", headers=HOST, json={"game_minutes": 3})
    assert client.post("/api/host/defaults", headers=HOST).json()["config"]["game_minutes"] == 25


def test_long_poll_wakes_up_when_a_turn_arrives(client):
    import threading
    a, b = reg(client, "Alpha"), reg(client, "Beta")
    client.get("/api/next?wait=0", headers=b)
    got = {}

    def poll():
        t0 = time.time()
        got["ev"] = client.get("/api/next?wait=15", headers=b).json()
        got["dt"] = time.time() - t0

    th = threading.Thread(target=poll)
    th.start()
    time.sleep(1.0)
    ea = client.get("/api/next?wait=0", headers=a).json()
    t_open = time.time()
    client.post("/api/act", headers=a, json={"event_id": ea["event_id"], "open": [{"to": "Beta", "message": "hi"}]})
    th.join(10)
    assert got["ev"]["type"] == "your_turn" and got["dt"] < 15
    assert time.time() - t_open < 2.0  # woke within moments, not at the 15 s timeout


def test_parallel_workers_get_different_events(client):
    """Two answer loops polling with the same key never receive the same event."""
    import threading
    client.post("/api/host/config", headers=HOST, json={"market_interval_s": 600})
    a, b, c = reg(client, "Alpha"), reg(client, "Beta"), reg(client, "Gamma")
    evs = {name: client.get("/api/next?wait=0", headers=h).json() for name, h in (("a", a), ("b", b), ("c", c))}
    for name, h in (("b", b), ("c", c)):
        r = client.post("/api/act", headers=h, json={"event_id": evs[name]["event_id"],
                                                     "open": [{"to": "Alpha", "message": "hi"}]}).json()
        assert r["ok"], r
    out = []

    def worker():
        out.append(client.get("/api/next?wait=3", headers=a).json())

    ths = [threading.Thread(target=worker) for _ in range(3)]
    [t.start() for t in ths]
    [t.join(10) for t in ths]
    turns = [e for e in out if e["type"] == "your_turn"]
    assert len(turns) == 2 and {e["partner"] for e in turns} == {"Beta", "Gamma"}
    assert [e["type"] for e in out].count("wait") == 1


AI_VARS = ("LLM", "LLM_COMMAND", "LLM_BASE_URL", "LLM_API_KEY", "MODEL", "OLLAMA_MODEL", "ANTHROPIC_API_KEY",
           "OPENAI_API_KEY", "GEMINI_API_KEY")


def load_bot(monkeypatch, **env):
    """Import a fresh copy of starter/bot.py with exactly these AI settings."""
    for k in AI_VARS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("EXCHANGE_URL", "http://unused")
    monkeypatch.setenv("TEAM_KEY", "k")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    path = os.path.join(os.path.dirname(__file__), "..", "starter", "bot.py")
    spec = importlib.util.spec_from_file_location("starter_bot_any_ai", path)
    bot = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bot)
    return bot


class FakeReply:
    def __init__(self, obj):
        self.obj, self.ok, self.status_code, self.text = obj, True, 200, json.dumps(obj)

    def json(self):
        return self.obj


def test_starter_bot_works_with_any_ai(monkeypatch, tmp_path):
    """Teams pick their own AI: Claude, OpenAI, Gemini, any OpenAI-style API, a local model, or a chat
    plan's command-line tool. The game server never sees that key."""
    # nothing set: a clear stop instead of a silent bot
    with pytest.raises(SystemExit, match="No AI set up"):
        load_bot(monkeypatch).check_brain()
    # an OpenAI-style API without a model name: the bot lists the models the key can use
    seen = {}

    def get(url, **kw):
        seen["get"] = (url, kw["headers"])
        return FakeReply({"data": [{"id": "models/m-small"}, {"id": "m-large"}]})

    bot = load_bot(monkeypatch, LLM_BASE_URL="http://ai.test/v1", LLM_API_KEY="sk-test")
    monkeypatch.setattr(bot.requests, "get", get)
    assert bot.BRAIN == "custom"
    with pytest.raises(SystemExit, match="m-large, m-small"):
        bot.check_brain()
    assert seen["get"] == ("http://ai.test/v1/models", {"Authorization": "Bearer sk-test"})
    # with a model: one chat call to <address>/chat/completions, carrying the team's AI key
    def post(url, **kw):
        seen["post"] = (url, kw["headers"], kw["json"])
        return FakeReply({"choices": [{"message": {"content": 'ok {"action": "talk", "message": "hi"}'}}]})

    bot = load_bot(monkeypatch, LLM_BASE_URL="http://ai.test/v1/", LLM_API_KEY="sk-test", MODEL="m-small")
    monkeypatch.setattr(bot.requests, "post", post)
    bot.check_brain()
    assert bot.decide({"type": "your_turn", "event_id": "t-1-1"}) == {"action": "talk", "message": "hi"}
    url, headers, body = seen["post"]
    assert url == "http://ai.test/v1/chat/completions" and headers == {"Authorization": "Bearer sk-test"}
    assert body["model"] == "m-small" and body["messages"][0]["role"] == "system"
    assert "Agent Briefing" in body["messages"][0]["content"] and '"your_turn"' in body["messages"][1]["content"]
    # OpenAI and Gemini take the same route at their own addresses
    assert load_bot(monkeypatch, OPENAI_API_KEY="x").BRAIN == "openai"
    g = load_bot(monkeypatch, GEMINI_API_KEY="x")
    assert g.BRAIN == "gemini" and g.OPENAI_STYLE["gemini"][1].startswith("https://generativelanguage.googleapis.com/")
    # LLM= picks one when several are set
    assert load_bot(monkeypatch, OPENAI_API_KEY="x", GEMINI_API_KEY="y", LLM="gemini").BRAIN == "gemini"
    with pytest.raises(SystemExit, match="Unknown LLM"):
        load_bot(monkeypatch, LLM="chatgpt").check_brain()
    # a chat plan's command-line tool (like claude -p): the prompt on stdin, the answer on stdout
    tool = tmp_path / "tool.py"
    tool.write_text("import sys\nn = len(sys.stdin.read())\nprint('Sure. {\"action\": \"talk\", \"message\": \"%d\"}' % n)\n")
    bot = load_bot(monkeypatch, LLM_COMMAND=f"{sys.executable} {tool}")
    assert bot.BRAIN == "command"
    bot.check_brain()
    ans = bot.decide({"type": "your_turn", "event_id": "t-1-2"})
    assert ans["action"] == "talk" and int(ans["message"]) > 2000  # the whole briefing went in
    broken = tmp_path / "broken.py"
    broken.write_text("import sys\nsys.stdin.read()\nsys.exit('not logged in')\n")
    with pytest.raises(ValueError, match="not logged in"):
        load_bot(monkeypatch, LLM_COMMAND=f"{sys.executable} {broken}").ask_llm("s", "u")
