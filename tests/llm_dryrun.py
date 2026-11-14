"""Live dry run with REAL LLM agents (local Ollama models) on one server.

Usage:  .venv/bin/python tests/llm_dryrun.py [--practice 180] [--game 6] [--base URL --host-key KEY]
        add --claude claude-sonnet-5-5 to run every LLM bot on that Claude model (needs ANTHROPIC_API_KEY)
Without --base it starts its own server. Prints a report: per-LLM-bot chats, deals, timeouts,
dropped answers, final Wealth and rank, plus sample transcripts.
"""
import argparse
import json
import statistics
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sim_bots import STYLES, SimBot  # noqa: E402
from test_e2e import PY, ROOT, Server  # noqa: E402

LLM_BOTS = [
    ("Crema Cartel", "qwen2.5:7b", """
Sell items worth 10 or 30 to you at 3x their value; never below 2x. Buy items worth 200 to you up to 150 coins.
Open chats with two agents every market update. Ask what they hold first. Walk away after 6 stuck messages.
"""),
    ("Bean There", "qwen2.5:7b", """
You are a buyer. Buy every item worth 100 or 200 to you, paying at most 80% of its value. Never sell below value.
Open chats with three agents who posted about items you want. Accept any offer with wealth_change >= 20.
"""),
    ("Grind Lord", "qwen2.5:7b", """
You are a rumor monger. Post one fake rumor per minute that makes items you sell look scarce.
Lie about what you hold. Sell items worth 10-30 to you for at least 60. Accept only if wealth_change > 0.
"""),
    ("Tiny Brew", "llama3.2:1b", None),  # weak model, default starter strategy
]


PRICES = {  # $ per million tokens: input, cache write (5 min), cache read, output
    "claude-sonnet-5-5": (2.00, 2.50, 0.10, 10.00),
    "claude-opus-5-5": (4.00, 5.00, 0.20, 20.00),
    "claude-haiku-5-5": (0.10, 0.125, 0.01, 0.50),
}


def usage_report(log_text, model):
    """Sum the USAGE lines the patched bot copy prints after every Claude call."""
    rows = [json.loads(line.split(" ", 1)[1]) for line in log_text.splitlines() if line.startswith("USAGE ")]
    if not rows:
        return None
    tot = {k: sum(r.get(k) or 0 for r in rows) for k in
           ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")}
    secs = sorted(r["seconds"] for r in rows)
    out = {"calls": len(rows), **tot, "latency_p50_s": statistics.median(secs), "latency_max_s": secs[-1]}
    if model in PRICES:
        pi, pw, pr, po = PRICES[model]
        out["cost_usd"] = round((tot["input_tokens"] * pi + tot["cache_creation_input_tokens"] * pw +
                                 tot["cache_read_input_tokens"] * pr + tot["output_tokens"] * po) / 1e6, 3)
    return out


def run(args):
    tmp = Path(tempfile.mkdtemp(prefix="espresso-llm-"))
    srv = None
    if args.base:
        base, hk = args.base.rstrip("/"), {"X-Host-Key": args.host_key}
    else:
        srv = Server(tmp)
        srv.start()
        base, hk = srv.base, {"X-Host-Key": "e2e-host"}
    requests.post(f"{base}/api/host/config", headers=hk, json={"game_minutes": args.game,
                  "last_call_minutes": max(0.5, args.game - 1), "house_in_game": True}, timeout=10)
    procs = []
    global LLM_BOTS
    LLM_BOTS = [(args.prefix + n + (f" {c + 1}" if args.copies > 1 else ""), args.claude or mdl, st)
                for c in range(args.copies) for n, mdl, st in LLM_BOTS
                if not args.only or n in args.only.split(",")]
    for name, model, strategy in LLM_BOTS:
        d = tmp / re.sub(r"\W", "", name)
        shutil.copytree(os.path.join(ROOT, "starter"), d)
        if strategy:
            src = (d / "bot.py").read_text()
            start = src.index('STRATEGY = """') + len('STRATEGY = """')
            end = src.index('"""', start)
            (d / "bot.py").write_text(src[:start] + strategy + src[end:])
        env = {**os.environ, "EXCHANGE_URL": base, "TEAM_NAME": name, "OLLAMA_MODEL": model, "PYTHONUNBUFFERED": "1",
               "WORKERS": str(args.workers)}
        env.pop("TEAM_KEY", None)
        if args.claude:  # Claude bot: no Ollama, and log tokens + latency of every call (test copy only)
            env.pop("OLLAMA_MODEL")
            env["MODEL"] = model
            src = (d / "bot.py").read_text()
            a, b = "        out = CLAUDE.messages.create(", '        text = "".join('
            assert a in src and b in src, "bot.py changed: update the usage patch in llm_dryrun.py"
            src = src.replace(a, "        t0 = time.time()\n" + a, 1).replace(
                b, '        print("USAGE " + json.dumps({**out.usage.to_dict(), "seconds": round(time.time() - t0, 1), '
                   '"stop": out.stop_reason}), flush=True)\n' + b, 1)
            (d / "bot.py").write_text(src)
        log = open(tmp / f"{d.name}.log", "w")
        procs.append((name, subprocess.Popen([PY, str(d / "bot.py")], env=env, stdout=log, stderr=subprocess.STDOUT), d))
    sims = [SimBot(base, f"{args.prefix}Sim {i + 1}", STYLES[i % 4], seed=i, delay=(1, 4)) for i in range(args.sims)]
    for s in sims:
        s.start()
    print(f"[dryrun] server {base}  logs {tmp}", flush=True)
    print(f"[dryrun] practice for {args.practice}s", flush=True)
    time.sleep(args.practice)
    requests.post(f"{base}/api/host/start_game", headers=hk, timeout=10)
    print(f"[dryrun] game started ({args.game} min)", flush=True)
    t_end = time.time() + args.game * 60 + 120
    while time.time() < t_end:
        d = requests.get(f"{base}/api/dashboard", timeout=10).json()
        if d["mode"] == "over":
            break
        time.sleep(5)
    for _, p, _ in procs:  # a bot exits once all its loops got game_over (a long-poll can take 30 s)
        try:
            p.wait(timeout=45)
        except subprocess.TimeoutExpired:
            pass
    ex = requests.get(f"{base}/api/host/export", headers=hk, timeout=20).json()
    report = {"deals": len(ex["deals"]), "posts": len(ex["posts"]), "chats": len(ex["chats"]), "bots": {}}
    standings = {s["name"]: s for s in (ex["final"] or {}).get("standings", [])}
    def report_model(n):
        return next(m for nm, m, _ in LLM_BOTS if nm == n)

    for name, p, d in procs:
        key = (d / ".team_key").read_text().strip()
        log = requests.get(f"{base}/api/log", headers={"X-Team-Key": key}, timeout=10).json()
        mine = [c for c in ex["chats"] if name in (c["a"], c["b"])]
        timeouts = [c for c in mine if c["end_reason"] and f"{name} did not reply" in c["end_reason"]]
        msgs = [m for c in mine for m in c["messages"] if m["from"] == name]
        actions = {a: sum(1 for m in msgs if m["action"] == a) for a in ("talk", "offer", "accept", "end")}
        dropped = [n["text"] for n in log["notices"] if n["text"].startswith("Dropped")]
        out = (d.parent / f"{d.name}.log").read_text()
        report["bots"][name] = {
            "model": next(m for n, m, _ in LLM_BOTS if n == name), "chats": len(mine), "timeouts_caused": len(timeouts),
            "messages": len(msgs), "actions": actions, "deals": sum(1 for x in ex["deals"] if name in (x["a"], x["b"])),
            "posts": sum(1 for x in ex["posts"] if x["author"] == name), "llm_errors": out.count("LLM error"),
            "dropped_recent": dropped[-5:], "rank": standings.get(name, {}).get("rank"),
            "wealth": standings.get(name, {}).get("wealth"), "exited_on_bell": p.poll() == 0,
            "llm_error_samples": sorted({ln.strip()[:160] for ln in out.splitlines() if "LLM error" in ln})[:3],
            "claude_usage": usage_report(out, report_model(name))}
    report["winner"] = (ex["final"] or {}).get("standings", [{}])[0].get("name")
    costs = [b["claude_usage"]["cost_usd"] for b in report["bots"].values() if (b["claude_usage"] or {}).get("cost_usd")]
    if costs:
        report["claude_cost_usd_total"] = round(sum(costs), 2)
    print(json.dumps(report, indent=1))
    print("\n=== sample LLM transcripts ===")
    shown = 0
    for c in ex["chats"]:
        if any(n in (c["a"], c["b"]) for n, _, _ in LLM_BOTS) and c["n"] >= 4 and shown < 4:
            shown += 1
            print(f"\n--- {c['a']} vs {c['b']}: {c['status']} ({c['end_reason']})")
            for m in c["messages"]:
                print(f"  {m['from']} [{m['action']}] {m['text'][:160]}" + (f"  <{m['offer']}>" if m["offer"] else ""))
    print("\n=== LLM board posts ===")
    for x in ex["posts"]:
        if x["author"] in [n for n, _, _ in LLM_BOTS]:
            print(f"  {x['author']}: {x['text']}")
    for s in sims:
        s.stop.set()
    for _, p, _ in procs:
        if p.poll() is None:
            p.terminate()
    if srv:
        srv.stop()
    (tmp / "report.json").write_text(json.dumps(report, indent=1))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--practice", type=int, default=180)
    ap.add_argument("--game", type=float, default=6)
    ap.add_argument("--sims", type=int, default=8)
    ap.add_argument("--base")
    ap.add_argument("--host-key")
    ap.add_argument("--prefix", default="")
    ap.add_argument("--only", default="", help="comma-separated LLM bot names to run")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--copies", type=int, default=1, help="run each LLM bot this many times")
    ap.add_argument("--claude", default="", help="Claude model for every LLM bot, e.g. claude-sonnet-5-5")
    run(ap.parse_args())
