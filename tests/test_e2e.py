"""End to end: a real server process, 20 agents on one platform, practice, a full game,
a server crash and restart in the middle of the game, the bell, the reveal.

Agents: 4 in-process house bots + 12 brain bots over HTTP + 1 chaos bot + 3 copies of the
shipped starter bot.py (self-registering by TEAM_NAME, talking to an Ollama-compatible fake LLM).
Run:  .venv/bin/python -m pytest tests/test_e2e.py -s
"""
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections import Counter

import pytest
import requests

from sim_bots import STYLES, ChaosBot, FakeLLM, SimBot

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(ROOT, ".venv", "bin", "python")
HOST = {"X-Host-Key": "e2e-host"}
ITEMS = ["beans", "grinder", "machine", "barista", "course"]
AI_VARS = {"TEAM_KEY", "LLM", "LLM_COMMAND", "LLM_BASE_URL", "LLM_API_KEY", "MODEL", "OLLAMA_MODEL", "OLLAMA_URL",
           "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"}


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Server:
    def __init__(self, tmp):
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.db = str(tmp / "e2e.db")
        self.logpath = str(tmp / "server.log")
        self.proc = None

    def start(self):
        env = {**os.environ, "DB_PATH": self.db, "HOST_KEY": "e2e-host", "SECRET_KEY": "e2e-secret"}
        log = open(self.logpath, "a")
        self.proc = subprocess.Popen([PY, "-m", "uvicorn", "server.app:app", "--host", "127.0.0.1",
                                      "--port", str(self.port), "--app-dir", ROOT],
                                     env=env, stdout=log, stderr=subprocess.STDOUT, cwd=ROOT)
        for _ in range(100):
            try:
                if requests.get(self.base + "/healthz", timeout=1).ok:
                    return
            except requests.RequestException:
                time.sleep(0.1)
        raise RuntimeError("server did not start")

    def kill(self):
        self.proc.send_signal(signal.SIGKILL)
        self.proc.wait()

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(10)


def host(srv, action, **body):
    r = requests.post(f"{srv.base}/api/host/{action}", headers=HOST, json=body, timeout=10)
    assert r.ok, r.text
    return r.json()


def export(srv):
    return requests.get(f"{srv.base}/api/host/export", headers=HOST, timeout=10).json()


def books(ex):
    coins = sum(a["coins"] for a in ex["agents"])
    start = sum(a["start_coins"] for a in ex["agents"])
    units = Counter()
    for a in ex["agents"]:
        units.update({k: v for k, v in a["items"].items()})
    return coins, start, units


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("e2e")
    srv = Server(tmp)
    srv.start()
    host(srv, "config", market_interval_s=4, reply_timeout_s=25, game_minutes=2.5, last_call_minutes=2.0,
         house_speed=0.15, house_in_game=True, redeliver_after_s=15)
    llm = FakeLLM(seed=1)
    starters = []
    for i in range(3):
        d = tmp / f"starter{i}"
        shutil.copytree(os.path.join(ROOT, "starter"), d)
        out = open(tmp / f"starter{i}.log", "w")
        env = {k: v for k, v in os.environ.items() if k not in AI_VARS}
        env.update({"EXCHANGE_URL": srv.base, "TEAM_NAME": f"LLM Bot {i + 1}", "PYTHONUNBUFFERED": "1"})
        llm_url = f"http://127.0.0.1:{llm.port}"
        env.update([  # three ways to give a bot a brain: local model, OpenAI-style API, chat-plan command
            {"OLLAMA_MODEL": "fake", "OLLAMA_URL": llm_url},
            {"LLM_BASE_URL": llm_url + "/v1", "LLM_API_KEY": "test-key", "MODEL": "fake"},
            {"LLM_COMMAND": f"{PY} {os.path.join(ROOT, 'tests', 'fake_llm_command.py')} {llm_url}"},
        ][i].items())
        starters.append((subprocess.Popen([PY, str(d / "bot.py")], env=env, stdout=out, stderr=subprocess.STDOUT),
                         tmp / f"starter{i}.log", d))
    bots = [SimBot(srv.base, f"Sim {i + 1}", STYLES[i % 4], seed=i) for i in range(12)]
    chaos = ChaosBot(srv.base, "Chaos Monkey", "Wildcard", seed=99)
    for b in bots + [chaos]:
        b.start()
    yield {"srv": srv, "llm": llm, "starters": starters, "bots": bots, "chaos": chaos, "tmp": tmp}
    for b in bots + [chaos]:
        b.stop.set()
    for p, _, _ in starters:
        if p.poll() is None:
            p.terminate()
    llm.close()
    srv.stop()


def test_twenty_agents_full_game_with_crash(world):
    srv, bots, chaos, starters, llm = world["srv"], world["bots"], world["chaos"], world["starters"], world["llm"]

    # ---- practice: everyone is in one market and trading
    time.sleep(25)
    dash = requests.get(srv.base + "/api/dashboard", timeout=10).json()
    names = {a["name"] for a in dash["agents"]}
    assert len(names) == 20, names
    assert {"LLM Bot 1", "LLM Bot 2", "LLM Bot 3", "Chaos Monkey", "Fair Trader", "Opener"} <= names
    assert dash["stats"]["deals"] > 0, "no deals in practice"
    for _, _, d in starters:
        assert (d / ".team_key").exists(), "starter bot should have registered itself"
    key = open(starters[0][2] / ".team_key").read().strip()
    log = requests.get(srv.base + "/api/log", headers={"X-Team-Key": key}, timeout=10).json()
    assert log["team"] == "LLM Bot 1" and log["chats"], "starter bot never chatted"

    # ---- game
    st = host(srv, "start_game")
    assert st["mode"] == "game"
    ex0 = export(srv)
    coins_start, _, units_start = books(ex0)
    assert len(ex0["agents"]) == 20 and all(a["wealth"] == 1500 for a in ex0["agents"])
    assert all(units_start[t] == 20 for t in ITEMS)

    time.sleep(55)
    mid = export(srv)
    deals_before_crash = len(mid["deals"])
    assert deals_before_crash > 5
    c, s, u = books(mid)
    assert c == coins_start and u == units_start, "books unbalanced mid-game"

    # ---- crash and restart mid-game
    srv.kill()
    time.sleep(3)
    srv.start()
    after = export(srv)
    assert after["mode"] == "game", "restart lost the game"
    assert len(after["deals"]) >= deals_before_crash
    c, s, u = books(after)
    assert c == coins_start and u == units_start, "books unbalanced after restart"

    # ---- run to the bell
    deadline = time.time() + 200
    while time.time() < deadline:
        d = requests.get(srv.base + "/api/dashboard", timeout=10).json()
        if d["mode"] == "over":
            break
        time.sleep(2)
    assert d["mode"] == "over", "bell never rang"
    time.sleep(6)

    fin = export(srv)
    c, s, u = books(fin)
    assert c == coins_start and u == units_start, "books unbalanced at the bell"
    st = fin["final"]["standings"]
    assert len(st) == 20 and [x["rank"] for x in st] == list(range(1, 21))
    assert all(st[i]["wealth"] >= st[i + 1]["wealth"] for i in range(19))
    assert len(fin["deals"]) > 20
    llm_names = {"LLM Bot 1", "LLM Bot 2", "LLM Bot 3"}
    llm_deals = [x for x in fin["deals"] if x["a"] in llm_names or x["b"] in llm_names]
    assert llm_deals, "the starter bots made no deal in the game"

    # every bot heard the bell
    for b in bots:
        assert b.stats["game_over"] and b.stats["game_over"]["rank"] >= 1, b.name
        assert b.stats["http429"] == 0, f"{b.name} was rate limited"
    sims = {b.name for b in bots}
    late = [c for c in fin["chats"] if c["end_reason"] and "did not reply" in c["end_reason"]
            and any(f"{n} did not reply" in c["end_reason"] for n in sims)]
    assert not late, f"fast bots timed out, so turns were lost or late: {[c['end_reason'] for c in late][:5]}"
    for p, logf, _ in starters:
        p.wait(30)
        out = open(logf).read()
        assert p.returncode == 0 and "Final result:" in out, out[-2000:]
    assert llm.calls > 30 and llm.bad > 0  # the fake LLM sent junk sometimes and the bots survived
    # every AI route was used, and the OpenAI-style route sent the team's AI key
    assert all(llm.routes.get(r, 0) > 5 for r in ("ollama", "openai-style", "command")), llm.routes
    assert llm.auth == {"Bearer test-key"}

    # chaos never broke the server, and was told why its answers were dropped
    assert chaos.stats["dropped"] > 5
    server_log = open(srv.logpath).read()
    assert "Traceback" not in server_log and " 500 " not in server_log, server_log[-3000:]

    # reveal and transcripts
    host(srv, "reveal")
    d = requests.get(srv.base + "/api/dashboard", timeout=10).json()
    assert d["mode"] == "reveal" and d["final"]["fun_facts"]
    t = requests.get(srv.base + "/api/transcripts", timeout=10)
    assert t.ok and "## Chats" in t.text and "LLM Bot 1" in t.text
    print("\nE2E summary:", {"deals": len(fin["deals"]), "posts": len(fin["posts"]), "llm_calls": llm.calls,
                             "llm_bad": llm.bad, "llm_routes": llm.routes, "llm_deals": len(llm_deals), "winner": st[0]["name"],
                             "chaos_dropped": chaos.stats["dropped"], "fun_facts": list(d["final"]["fun_facts"])})
