"""Class-scale load check for version 2: a real server, 25 teams of scripted bots over HTTP, each with 2 shell
companies funded by private transfers, a compressed game with news, tips, house orders and the pulse, plus 30
spectator screens polling like the projector. Prints latency and size numbers and PASS or FAIL.

Run:  .venv/bin/python tests/load_v2.py --teams 25 --minutes 3
"""
from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from server.house_bots import Brain  # noqa: E402
from test_e2e import HOST, Server  # noqa: E402

STYLES = ["Fair Trader", "Hardliner", "Wildcard", "Opener"]
lock = threading.Lock()
stats = {"act_ms": [], "next_ms": [], "max_event": 0, "5xx": 0, "errors": 0, "events": 0, "health_ms": [],
         "screen_ms": [], "shells": 0, "transfers": 0}


def p95(xs):
    return sorted(xs)[int(len(xs) * 0.95) - 1] if xs else 0


def bot(base: str, i: int, stop: threading.Event):
    s = requests.Session()
    name = f"Load {i:02d}"
    r = s.post(base + "/api/register", json={"team": name, "members": [f"Load {chr(65 + i // 26)}{chr(65 + i % 26)} "
                                                                          f"Student"]}, timeout=20).json()
    h = {"X-Team-Key": r["key"]}
    for k in range(2):
        if s.post(base + "/api/shell", json={"name": f"Shell {i:02d}{'ab'[k]}"}, headers=h, timeout=20).ok:
            with lock:
                stats["shells"] += 1
    brains = {}
    funded = False
    rng = random.Random(i)
    while not stop.is_set():
        t0 = time.time()
        try:
            resp = s.get(base + "/api/next", params={"wait": 10}, headers=h, timeout=40)
        except requests.RequestException:
            with lock:
                stats["errors"] += 1
            continue
        with lock:
            stats["next_ms"].append((time.time() - t0) * 1000)
            stats["max_event"] = max(stats["max_event"], len(resp.content))
            if resp.status_code >= 500:
                stats["5xx"] += 1
        ev = resp.json()
        if ev.get("type") == "game_over":
            return
        if ev.get("type") not in ("market", "your_turn"):
            continue
        with lock:
            stats["events"] += 1
        you = ev["you"]
        brain = brains.setdefault(you, Brain(STYLES[(i + len(brains)) % 4], random.Random(i * 7 + len(brains))))
        ans = brain.decide(ev)
        if ev["type"] == "market" and not funded and ev.get("team", {}).get("you_are") == "main":
            shells = [e["name"] for e in ev["team"]["entities"] if e["role"] == "shell"]
            ans["transfer"] = [{"to": n, "give": {"coins": 100 + rng.randint(0, 100)}} for n in shells]
            funded = True
        ans["event_id"] = ev["event_id"]
        time.sleep(rng.uniform(0.3, 2.0))  # a fast LLM
        t0 = time.time()
        try:
            out = s.post(base + "/api/act", json=ans, headers=h, timeout=20)
        except requests.RequestException:
            with lock:
                stats["errors"] += 1
            continue
        with lock:
            stats["act_ms"].append((time.time() - t0) * 1000)
            if out.status_code >= 500:
                stats["5xx"] += 1
            if "transfer" in ans and any("private transfer" in x for x in out.json().get("applied", [])):
                stats["transfers"] += 1


def screen(base: str, sk: str, stop: threading.Event, k: int):
    s = requests.Session()
    hdr = {"X-Screen-Key": sk} if k == 0 else {}  # one projector, the rest are student laptops
    while not stop.is_set():
        t0 = time.time()
        try:
            s.get(base + "/api/dashboard", headers=hdr, timeout=10)
            s.get(base + "/api/watch/chats", headers=hdr, timeout=10)
            with lock:
                stats["screen_ms"].append((time.time() - t0) * 1000)
        except requests.RequestException:
            with lock:
                stats["errors"] += 1
        time.sleep(2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--teams", type=int, default=25)
    ap.add_argument("--minutes", type=float, default=3)
    ap.add_argument("--screens", type=int, default=30)
    args = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="esx-load-"))
    srv = Server(tmp)
    srv.start()
    base = srv.base
    try:
        m = args.minutes
        requests.post(base + "/api/host/config", headers=HOST, timeout=10, json={
            "game_minutes": m + 0.5, "last_call_minutes": m, "tip_lead_s": 15, "house_order_first_s": 15,
            "house_order_every_s": 30, "house_order_window_s": 20, "pulse_every_s": 40, "market_interval_s": 15})
        stop = threading.Event()
        bots = [threading.Thread(target=bot, args=(base, i, stop), daemon=True) for i in range(args.teams)]
        for t in bots:
            t.start()
        time.sleep(8)
        requests.post(base + "/api/host/start_game", headers=HOST, timeout=10)
        sk = requests.get(base + "/api/host/state", headers=HOST, timeout=10).json()["screen_key"]
        screens = [threading.Thread(target=screen, args=(base, sk, stop, k), daemon=True) for k in range(args.screens)]
        for t in screens:
            t.start()
        end = time.time() + m * 60
        while time.time() < end:
            t0 = time.time()
            try:
                requests.get(base + "/healthz", timeout=5)
                stats["health_ms"].append((time.time() - t0) * 1000)
            except requests.RequestException:
                stats["health_ms"].append(5000)
            time.sleep(1)
        exp = requests.get(base + "/api/host/export", headers=HOST, timeout=30).json()
        stop.set()
        v2 = exp.get("v2", {})
        summary = {
            "teams": args.teams, "shells": stats["shells"], "transfers": stats["transfers"], "events": stats["events"],
            "deals": len(exp["deals"]), "house_fills": sum(1 for d in exp["deals"] if d.get("kind") == "house_fill"),
            "news": sum(1 for n in v2.get("news_plan", []) if n.get("done")),
            "p95_act_ms": round(p95(stats["act_ms"])), "median_act_ms": round(statistics.median(stats["act_ms"] or [0])),
            "p95_screen_poll_ms": round(p95(stats["screen_ms"])), "max_health_ms": round(max(stats["health_ms"] or [0])),
            "max_event_kb": round(stats["max_event"] / 1024, 1), "5xx": stats["5xx"], "errors": stats["errors"]}
        print(json.dumps(summary, indent=1))
        ok = (summary["p95_act_ms"] < 300 and summary["max_event_kb"] < 24 and summary["5xx"] == 0
              and summary["max_health_ms"] < 1000 and summary["shells"] == 2 * args.teams)
        server_log = open(srv.logpath).read()
        if "Traceback" in server_log:
            ok = False
            print(server_log[server_log.index("Traceback"):][:2000])
        print("PASS" if ok else "FAIL")
    finally:
        srv.kill()


if __name__ == "__main__":
    main()
