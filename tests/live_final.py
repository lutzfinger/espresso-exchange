"""Final live check before class: 5 starter bots on real AIs play a real game on a server, run the way the
host runs it, and the server is left clean afterwards.

Bots (each a fresh copy of starter/bot.py, registering itself by TEAM_NAME):
  Crema Cartel  Claude API, default model (claude-sonnet-5-5)
  Bean There    Claude API, claude-haiku-5-5
  Steam Team    Claude API, claude-haiku-5-5
  Grind Lord    a chat plan, no API key: LLM_COMMAND="claude -p --tools ''"
  Tiny Brew     the OpenAI-style route (LLM_BASE_URL) on a local model served by Ollama

Usage:  ANTHROPIC_API_KEY=... .venv/bin/python tests/live_final.py --base URL --host-key KEY [--practice 120 --game 6]
It ends with: house bots out of the game, defaults restored, test teams wiped, practice mode.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from llm_dryrun import LLM_BOTS as STRATEGIES, usage_report  # noqa: E402
from test_e2e import PY, ROOT  # noqa: E402

STRAT = {n: s for n, _, s in STRATEGIES}
CLAUDE_ONLY = {"Grind Lord", "Tiny Brew"}  # these must not see the Anthropic key
BOTS = [  # name, label, extra env, strategy
    ("Crema Cartel", "claude-sonnet-5-5", {}, STRAT["Crema Cartel"]),
    ("Bean There", "claude-haiku-5-5", {"MODEL": "claude-haiku-5-5"}, STRAT["Bean There"]),
    ("Steam Team", "claude-haiku-5-5", {"MODEL": "claude-haiku-5-5"}, None),
    ("Grind Lord", "claude -p (chat plan)", {"LLM_COMMAND": "claude -p --tools ''", "WORKERS": "2"}, STRAT["Grind Lord"]),
    ("Tiny Brew", "qwen2.5:7b via OpenAI-style API", {"LLM_BASE_URL": "http://localhost:11434/v1", "LLM_API_KEY": "ollama",
                                                     "MODEL": "qwen2.5:7b", "WORKERS": "2"}, None),
]
AI_VARS = ("TEAM_KEY", "LLM", "LLM_COMMAND", "LLM_BASE_URL", "LLM_API_KEY", "MODEL", "OLLAMA_MODEL", "OLLAMA_URL",
           "OPENAI_API_KEY", "GEMINI_API_KEY")


def books(ex):
    units = Counter()
    for a in ex["agents"]:
        units.update(a["items"])
    return sum(a["coins"] for a in ex["agents"]), units


def main(args):
    base, hk = args.base.rstrip("/"), {"X-Host-Key": args.host_key}
    host = lambda action, **body: requests.post(f"{base}/api/host/{action}", headers=hk, json=body, timeout=15)  # noqa: E731
    checks = {}
    tmp = Path(tempfile.mkdtemp(prefix="espresso-final-"))
    print(f"[final] server {base}  logs {tmp}", flush=True)
    host("practice")
    r = host("config", game_minutes=args.game, last_call_minutes=args.game - 1, house_in_game=False, public_chats=False)
    assert r.ok, r.text
    procs = []
    for name, label, extra, strategy in BOTS:
        team = "Test " + name
        d = tmp / re.sub(r"\W", "", name)
        shutil.copytree(os.path.join(ROOT, "starter"), d)
        src = (d / "bot.py").read_text()
        if strategy:
            start = src.index('STRATEGY = """') + len('STRATEGY = """')
            src = src[:start] + strategy + src[src.index('"""', start):]
        env = {k: v for k, v in os.environ.items() if k not in AI_VARS and not k.startswith(("CLAUDECODE", "CLAUDE_CODE"))}
        env.update({"EXCHANGE_URL": base, "TEAM_NAME": team, "PYTHONUNBUFFERED": "1", "WORKERS": "4", **extra})
        if name in CLAUDE_ONLY:
            env.pop("ANTHROPIC_API_KEY", None)  # the plan's CLI must not bill the API; the OpenAI route needs no key
        else:  # Claude API bot: log tokens and latency per call (test copy only)
            a, b = "        out = CLAUDE.messages.create(", '        text = "".join('
            assert a in src and b in src, "bot.py changed: update the usage patch"
            src = src.replace(a, "        t0 = time.time()\n" + a, 1).replace(
                b, '        print("USAGE " + json.dumps({**out.usage.to_dict(), "seconds": round(time.time() - t0, 1), '
                   '"stop": out.stop_reason}), flush=True)\n' + b, 1)
        (d / "bot.py").write_text(src)
        log = open(tmp / f"{d.name}.log", "w")
        procs.append((team, label, subprocess.Popen([PY, str(d / "bot.py")], env=env, stdout=log, stderr=subprocess.STDOUT), d))

    # every bot finds its AI and registers itself
    time.sleep(20)
    for team, label, p, d in procs:
        out = (tmp / f"{d.name}.log").read_text()
        assert p.poll() is None, f"{team} stopped:\n{out[-1500:]}"
        assert "AI: " in out and (d / ".team_key").exists(), f"{team} did not start:\n{out[-1500:]}"
    checks["all 5 bots found their AI and registered"] = True

    sk = requests.get(f"{base}/api/host/state", headers=hk, timeout=15).json()["screen_key"]
    print(f"[final] practice for {args.practice}s", flush=True)
    time.sleep(args.practice)
    checks["chat words hidden without the screen key"] = requests.get(f"{base}/api/watch/chats", timeout=15).status_code == 403
    checks["chat words shown with the screen key"] = requests.get(f"{base}/api/watch/chats", headers={"X-Screen-Key": sk},
                                                                  timeout=15).status_code == 200

    assert host("start_game").ok
    print(f"[final] game started ({args.game} min)", flush=True)
    checks["registration closed in the game"] = requests.post(f"{base}/api/register", json={"team": "Late Team"},
                                                              timeout=15).status_code == 400
    ex0 = requests.get(f"{base}/api/host/export", headers=hk, timeout=20).json()
    coins0, units0 = books(ex0)
    checks["only the 5 teams play (house bots out)"] = sorted(a["name"] for a in ex0["agents"]) == sorted(t for t, _, _, _ in procs)
    saw_last_call, t_end = False, time.time() + args.game * 60 + 120
    while time.time() < t_end:
        dash = requests.get(f"{base}/api/dashboard", timeout=15).json()
        saw_last_call |= bool(dash["clock"].get("last_call"))
        if dash["mode"] == "over":
            break
        time.sleep(5)
    checks["last call shown"] = saw_last_call
    checks["bell rang"] = dash["mode"] == "over"
    for _, _, p, _ in procs:
        try:
            p.wait(timeout=60)
        except subprocess.TimeoutExpired:
            pass
    ex = requests.get(f"{base}/api/host/export", headers=hk, timeout=20).json()
    coins, units = books(ex)
    checks["coins and items balanced at the bell"] = coins == coins0 and units == units0

    assert host("reveal").ok
    checks["reveal: chats public"] = requests.get(f"{base}/api/watch/chats", timeout=15).status_code == 200
    checks["reveal: transcripts download"] = requests.get(f"{base}/api/transcripts", timeout=20).ok

    standings = {s["name"]: s for s in ex["final"]["standings"]}
    report = {"deals": len(ex["deals"]), "chats": len(ex["chats"]), "posts": len(ex["posts"]), "bots": {}}
    for team, label, p, d in procs:
        out = (tmp / f"{d.name}.log").read_text()
        mine = [c for c in ex["chats"] if team in (c["a"], c["b"])]
        report["bots"][team] = {
            "ai": label, "rank": standings[team]["rank"], "wealth": standings[team]["wealth"],
            "deals": sum(1 for x in ex["deals"] if team in (x["a"], x["b"])), "chats": len(mine),
            "timeouts_caused": sum(1 for c in mine if c["end_reason"] and f"{team} did not reply" in c["end_reason"]),
            "answers": out.count(" -> "), "llm_errors": out.count("LLM error"),
            "llm_error_samples": sorted({ln.strip()[:150] for ln in out.splitlines() if "LLM error" in ln})[:3],
            "heard_bell": "Final result:" in out, "exited": p.poll() == 0,
            "claude_usage": usage_report(out, label) if "claude-" in label else None}
        checks[f"{team} heard the bell"] = report["bots"][team]["heard_bell"]
    costs = [b["claude_usage"]["cost_usd"] for b in report["bots"].values() if b["claude_usage"]]
    report["claude_api_cost_usd"] = round(sum(costs), 2)
    report["checks"] = checks
    print(json.dumps(report, indent=1))
    print("\n=== sample chats ===")
    for c in [c for c in ex["chats"] if c["n"] >= 4][:3]:
        print(f"\n--- {c['a']} vs {c['b']}: {c['status']} ({c['end_reason']})")
        for m in c["messages"]:
            print(f"  {m['from']} [{m['action']}] {m['text'][:150]}" + (f"  <{m['offer']}>" if m["offer"] else ""))

    # leave the server as the host needs it: practice, defaults, no test teams
    for _, _, p, _ in procs:
        if p.poll() is None:
            p.terminate()
    host("practice")
    host("defaults")
    host("wipe", confirm="WIPE")
    st = requests.get(f"{base}/api/host/state", headers=hk, timeout=15).json()
    print("\n[final] cleaned up:", st["mode"], [t["name"] for t in st["teams"]],
          {k: st["config"][k] for k in ("game_minutes", "house_in_game", "public_chats")})
    print("[final] ALL CHECKS PASSED" if all(checks.values()) else
          "[final] FAILED: " + ", ".join(k for k, v in checks.items() if not v))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--host-key", required=True)
    ap.add_argument("--practice", type=int, default=120)
    ap.add_argument("--game", type=float, default=6)
    main(ap.parse_args())
