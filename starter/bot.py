# bot.py: The Espresso Exchange starter bot. Python 3.9 or newer, nothing to install.
#
# Easiest way, with a paid AI plan and no API key:
#   1. Register your team on the game website (team name + every member's full name).
#   2. Save your team key in a file named .team_key next to this file (or: export TEAM_KEY=<key>).
#   3. Run:  python3 bot.py
#   The bot finds the "claude" command (Claude Code, Claude plan) or the "codex" command (OpenAI Codex,
#   ChatGPT plan) and asks that plan for every decision.
#
# Other brains: set ONE of these instead
#   Claude API           export ANTHROPIC_API_KEY=...          (needs: pip install anthropic)
#   OpenAI API           export OPENAI_API_KEY=...   MODEL=<model name>
#   Gemini API           export GEMINI_API_KEY=...   MODEL=<model name>
#   Other OpenAI-style APIs (Groq, Mistral, OpenRouter, DeepSeek, ...):
#                        export LLM_BASE_URL=<their API address>  LLM_API_KEY=...  MODEL=<model name>
#   A model on your laptop (Ollama)    export OLLAMA_MODEL=qwen2.5:7b
#   Any command that reads the prompt on standard input and prints the answer:
#                        export LLM_COMMAND="..."
# Forgot the model name? Leave MODEL out and the bot lists the models your key can use.
# Several AI settings at once? export LLM=claude-plan|codex-plan|claude|openai|gemini|custom|ollama|command picks one.
# No key yet? export TEAM_NAME="Crema Cartel" TEAM_MEMBERS="Ana Lopez, Ben Wu" and the first run registers you.
# The game address is built in when you download this file; EXCHANGE_URL overrides it.
import json, os, shlex, shutil, subprocess, sys, threading, time, urllib.error, urllib.request

sys.stdout.reconfigure(line_buffering=True)     # bot.log fills line by line, even when it runs in the background

SERVER = os.environ.get("EXCHANGE_URL", "<URL>").rstrip("/")
HERE = os.path.dirname(os.path.abspath(__file__))
WORKERS = int(os.environ.get("WORKERS", "3"))  # chats answered in parallel; 1 = one event at a time
MODEL = os.environ.get("MODEL")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL")
OPENAI_STYLE = {  # brain: (key variable, API address) for OpenAI, Gemini and every OpenAI-style API
    "openai": ("OPENAI_API_KEY", "https://api.openai.com/v1"),
    "gemini": ("GEMINI_API_KEY", "https://generativelanguage.googleapis.com/v1beta/openai"),
    "custom": ("LLM_API_KEY", os.environ.get("LLM_BASE_URL", "")),
}


def pick_brain():
    if os.environ.get("LLM"):
        return os.environ["LLM"].strip().lower()
    for brain, var in (("command", "LLM_COMMAND"), ("ollama", "OLLAMA_MODEL"), ("claude", "ANTHROPIC_API_KEY"),
                       ("custom", "LLM_BASE_URL"), ("openai", "OPENAI_API_KEY"), ("gemini", "GEMINI_API_KEY")):
        if os.environ.get(var):
            return brain
    if shutil.which("claude"):                   # Claude Code is installed: use the paid plan behind it
        return "claude-plan"
    if shutil.which("codex"):                    # OpenAI Codex is installed: use the ChatGPT plan behind it
        return "codex-plan"
    return None


BRAIN = pick_brain()
if BRAIN == "claude":
    from anthropic import Anthropic
    CLAUDE = Anthropic()                                       # reads ANTHROPIC_API_KEY

# ============ EDIT THIS: your team's negotiation strategy ============
STRATEGY = """
Never sell an item below its value_now. Buy items whose value_now is 100 or more.
Open every chat by asking what they hold. Anchor high, concede slowly.
Sell items you value under the house order cap into house orders.
Post one rumor on the board that helps you. Walk away after 6 stuck messages.
"""
# =====================================================================


def _http(method, url, body=None, headers=None, timeout=40):
    """One HTTP call with the standard library: (status code, parsed JSON or None, raw text)."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status, text = r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:              # 4xx/5xx still carry the server's JSON explanation
        status, text = e.code, e.read().decode("utf-8", "replace")
    try:
        return status, json.loads(text), text
    except ValueError:
        return status, None, text


def team_key():
    if os.environ.get("TEAM_KEY"):
        return os.environ["TEAM_KEY"].strip()
    path = os.path.join(HERE, ".team_key")
    if os.path.exists(path):
        return open(path).read().strip().strip("'\"").strip()   # Windows echo keeps the quotes
    if not os.environ.get("TEAM_NAME") or not os.environ.get("TEAM_MEMBERS"):
        raise SystemExit("No team key. Register on the game website and save the key in .team_key next to "
                         "bot.py, or set TEAM_NAME and TEAM_MEMBERS (every member's full name, comma separated).")
    _, r, text = _http("POST", f"{SERVER}/api/register", {"team": os.environ["TEAM_NAME"],
                                                         "members": os.environ["TEAM_MEMBERS"]}, timeout=20)
    if not r or "key" not in r:
        raise SystemExit("Registration failed: " + str((r or {}).get("error") or text[:200]))
    open(path, "w").write(r["key"])
    print("Registered as", r["team"], "- key saved in .team_key")
    return r["key"]


HEADERS = {}                                    # filled in by main(): {"X-Team-Key": ...}
BRIEFING = open(os.path.join(HERE, "briefing.md")).read()
FORMAT = {
    "your_turn": '{"message": "...", "action": "talk|offer|accept|end", '
                 '"offer": {"give": {...}, "get": {...}} or null, "post": null}',
    "market": '{"open": [{"to": "<agent name>", "message": "...", "offer": null}], '
              '"post": null}',
}
EXTRAS = ('You may add any of these to the same JSON object (leave them out when not needed): "house_ask", '
          '"transfer", "audit", "create_shell", "contract", "contract_reply". The briefing explains each one.')
CLAUDE_PLAN = ["claude", "-p", "--tools", "", "--strict-mcp-config", "--no-session-persistence",
               "--setting-sources", "project"]
CODEX_PLAN = ["codex", "exec", "--skip-git-repo-check", "-"]   # read-only by default; the prompt comes on stdin


def ask_llm(system, user):
    if BRAIN == "claude-plan":               # Claude Code on a paid plan: no tools, our briefing as the system
        args = CLAUDE_PLAN + ["--model", MODEL or "sonnet", "--system-prompt", system]
        args[0] = shutil.which(args[0]) or args[0]
        out = subprocess.run(args, input=user, capture_output=True, text=True, timeout=80, cwd=HERE)
        if not out.stdout.strip():
            raise ValueError("no answer from claude: " + (out.stderr.strip() or "is Claude Code logged in?")[-300:])
        return out.stdout
    if BRAIN in ("command", "codex-plan"):   # a chat plan's command-line tool: prompt in, answer out
        args = shlex.split(os.environ["LLM_COMMAND"]) if BRAIN == "command" else \
            CODEX_PLAN[:-1] + (["--model", MODEL] if MODEL else []) + CODEX_PLAN[-1:]
        args[0] = shutil.which(args[0]) or args[0]
        out = subprocess.run(args, input=system + "\n\n" + user, capture_output=True, text=True, timeout=80)
        if not out.stdout.strip():
            raise ValueError("no answer from " + ("LLM_COMMAND: " if BRAIN == "command" else "codex: ")
                             + out.stderr.strip()[-300:])
        return out.stdout
    if BRAIN == "ollama":
        _, r, text = _http("POST", os.environ.get("OLLAMA_URL", "http://localhost:11434") + "/api/chat",
                           {"model": OLLAMA_MODEL, "stream": False, "format": "json",
                            "messages": [{"role": "system", "content": system},
                                         {"role": "user", "content": user}]}, timeout=120)
        return r["message"]["content"]
    if BRAIN == "claude":
        out = CLAUDE.messages.create(
            model=MODEL or "claude-sonnet-5-5", max_tokens=4000, messages=[{"role": "user", "content": user}],
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}])  # cached = cheaper
        text = "".join(b.text for b in out.content if b.type == "text")   # skip thinking blocks
        if not text:
            raise ValueError(f"no answer from the model (stop reason: {out.stop_reason})")
        return text
    key_var, base = OPENAI_STYLE[BRAIN]      # OpenAI, Gemini and every OpenAI-style API
    status, r, text = _http("POST", base.rstrip("/") + "/chat/completions", timeout=80,
                            headers={"Authorization": "Bearer " + os.environ.get(key_var, "")},
                            body={"model": MODEL, "messages": [{"role": "system", "content": system},
                                                              {"role": "user", "content": user}]})
    if status != 200 or not r:
        raise ValueError(f"{status} from the AI: {text[:300]}")
    return r["choices"][0]["message"]["content"]


def check_brain():
    """Stop early, with a clear message, if the bot has no AI, no model name or no game address."""
    if not SERVER.startswith("http"):        # the download fills in the address; a raw copy has a placeholder
        raise SystemExit("Set EXCHANGE_URL to the game website (or download bot.py from the game website).")
    if BRAIN is None:
        raise SystemExit("No AI set up. Install Claude Code (Claude plan) or Codex (ChatGPT plan), or set ONE of "
                         "ANTHROPIC_API_KEY, OPENAI_API_KEY, GEMINI_API_KEY, LLM_BASE_URL, OLLAMA_MODEL or "
                         "LLM_COMMAND (see the top of bot.py).")
    if BRAIN not in ("claude-plan", "codex-plan", "command", "ollama", "claude", *OPENAI_STYLE):
        raise SystemExit(f"Unknown LLM={BRAIN}. Use claude-plan, codex-plan, claude, openai, gemini, custom, ollama "
                         f"or command.")
    if BRAIN in OPENAI_STYLE and not MODEL:
        key_var, base = OPENAI_STYLE[BRAIN]
        try:
            _, r, _ = _http("GET", base.rstrip("/") + "/models", timeout=20,
                            headers={"Authorization": "Bearer " + os.environ.get(key_var, "")})
            names = sorted(m["id"].split("/")[-1] for m in r["data"])
        except Exception:
            names = []
        raise SystemExit("Set MODEL to the model your bot should use"
                         + (", for example one of: " + ", ".join(names[:40]) if names else "."))
    print("AI:", BRAIN, "|", os.environ.get("LLM_COMMAND") or OLLAMA_MODEL or MODEL or
          {"claude-plan": "sonnet via your Claude plan", "codex-plan": "your ChatGPT plan via codex"}.get(
              BRAIN, "claude-sonnet-5-5"))


def decide(event):
    text = ask_llm(BRIEFING + "\n\n# Your strategy\n" + STRATEGY,
                   "Event:\n" + json.dumps(event, separators=(",", ":")) +   # compact: fewer tokens
                   "\n\nAnswer with JSON only, in this format:\n" + FORMAT[event["type"]] + "\n" + EXTRAS)
    return json.JSONDecoder().raw_decode(text[text.find("{"):])[0]   # first JSON object, ignore the rest


def worker(done):
    while not done.is_set():
        try:
            _, event, text = _http("GET", f"{SERVER}/api/next", headers=HEADERS, timeout=40)
            if event is None:
                raise ValueError(text[:200] or "empty answer")
        except (OSError, ValueError) as err:
            print("Server unreachable, retrying:", err)
            time.sleep(3)
            continue
        kind = event.get("type")
        if kind == "game_over":
            if not done.is_set():
                done.set()
                print("Final result:", event)
            return
        if kind == "error":                      # bad key or too many requests
            print("Server says:", event.get("error"))
            time.sleep(5)
            continue
        if kind not in FORMAT:                   # "wait": nothing to do yet
            continue
        try:
            answer = decide(event)
        except Exception as err:                 # never crash, never go silent
            print("LLM error:", err)
            answer = ({"message": "Give me a moment.", "action": "talk"}
                      if kind == "your_turn" else {})
        answer["event_id"] = event["event_id"]
        try:
            _, result, text = _http("POST", f"{SERVER}/api/act", answer, headers=HEADERS, timeout=20)
            result = result or {"error": text[:200]}
        except (OSError, ValueError) as err:
            result = {"error": str(err)}
        print(kind, ("as " + event.get("you", "") + " ") if event.get("team", {}).get("you_are") == "shell" else "",
              "->", answer.get("action", "market"), result)


def main():
    check_brain()                                # first: no AI, no registration
    HEADERS["X-Team-Key"] = team_key()
    done = threading.Event()                     # WORKERS loops share one key; the server
    loops = [threading.Thread(target=worker, args=(done,)) for _ in range(max(1, WORKERS))]
    for t in loops:                              # hands each loop a different event
        t.start()
    for t in loops:
        t.join()


if __name__ == "__main__":
    main()
