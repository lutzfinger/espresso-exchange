# bot.py: The Espresso Exchange starter bot
# Setup (about 15 minutes; the setup guide has every step):
#   pip install requests                  (and: pip install anthropic, if your bot uses Claude's API)
#   export EXCHANGE_URL=<the game website>
#   export TEAM_NAME="Crema Cartel"      (the bot announces this name to the server)
#   Give the bot a brain: set ONE of these
#     Claude API           export ANTHROPIC_API_KEY=...          (model claude-sonnet-5-5 unless you set MODEL)
#     OpenAI API           export OPENAI_API_KEY=...   MODEL=<model name>
#     Gemini API           export GEMINI_API_KEY=...   MODEL=<model name>
#     Other OpenAI-style APIs (Groq, Mistral, OpenRouter, DeepSeek, ...):
#                          export LLM_BASE_URL=<their API address>  LLM_API_KEY=...  MODEL=<model name>
#     A model on your laptop (Ollama)    export OLLAMA_MODEL=qwen2.5:7b
#     Your paid chat plan, no API key    export LLM_COMMAND="claude -p --tools ''"
#                          (any command that reads the prompt on standard input and prints the answer)
#   python bot.py
# Forgot the model name? Leave MODEL out and the bot lists the models your key can use.
# The first run registers TEAM_NAME and saves the team key in .team_key next to this file.
# Already have a team key? export TEAM_KEY=<key> instead of TEAM_NAME.
# Several AI settings at once? export LLM=claude|openai|gemini|custom|ollama|command picks one.
import json, os, shlex, shutil, subprocess, threading, time
import requests

SERVER = os.environ["EXCHANGE_URL"].rstrip("/")
HERE = os.path.dirname(os.path.abspath(__file__))
WORKERS = int(os.environ.get("WORKERS", "4"))  # chats answered in parallel; 1 = one event at a time
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
    return None


BRAIN = pick_brain()
if BRAIN == "claude":
    from anthropic import Anthropic
    CLAUDE = Anthropic()                                       # reads ANTHROPIC_API_KEY

# ============ EDIT THIS: your team's negotiation strategy ============
STRATEGY = """
Never sell an item below its value on your card. Buy items worth 100+ to you.
Open every chat by asking what they hold. Anchor high, concede slowly.
Post one rumor on the board that helps you. Walk away after 6 stuck messages.
"""
# =====================================================================

def team_key():
    if os.environ.get("TEAM_KEY"):
        return os.environ["TEAM_KEY"]
    path = os.path.join(HERE, ".team_key")
    if os.path.exists(path):
        return open(path).read().strip()
    r = requests.post(f"{SERVER}/api/register", json={"team": os.environ["TEAM_NAME"]}, timeout=20).json()
    if "key" not in r:
        raise SystemExit("Registration failed: " + str(r.get("error")))
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


def ask_llm(system, user):
    if BRAIN == "command":                   # a chat plan's command-line tool: prompt in, answer out
        args = shlex.split(os.environ["LLM_COMMAND"])
        args[0] = shutil.which(args[0]) or args[0]
        out = subprocess.run(args, input=system + "\n\n" + user, capture_output=True, text=True, timeout=80)
        if not out.stdout.strip():
            raise ValueError("no answer from LLM_COMMAND: " + out.stderr.strip()[-300:])
        return out.stdout
    if BRAIN == "ollama":
        r = requests.post(os.environ.get("OLLAMA_URL", "http://localhost:11434") + "/api/chat", timeout=120,
                          json={"model": OLLAMA_MODEL, "stream": False, "format": "json",
                                "messages": [{"role": "system", "content": system},
                                             {"role": "user", "content": user}]})
        return r.json()["message"]["content"]
    if BRAIN == "claude":
        out = CLAUDE.messages.create(
            model=MODEL or "claude-sonnet-5-5", max_tokens=4000, messages=[{"role": "user", "content": user}],
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}])  # cached = cheaper
        text = "".join(b.text for b in out.content if b.type == "text")   # skip thinking blocks
        if not text:
            raise ValueError(f"no answer from the model (stop reason: {out.stop_reason})")
        return text
    key_var, base = OPENAI_STYLE[BRAIN]      # OpenAI, Gemini and every OpenAI-style API
    r = requests.post(base.rstrip("/") + "/chat/completions", timeout=80,
                      headers={"Authorization": "Bearer " + os.environ.get(key_var, "")},
                      json={"model": MODEL, "messages": [{"role": "system", "content": system},
                                                       {"role": "user", "content": user}]})
    if not r.ok:
        raise ValueError(f"{r.status_code} from the AI: {r.text[:300]}")
    return r.json()["choices"][0]["message"]["content"]


def check_brain():
    """Stop early, with a clear message, if the bot has no AI or no model name."""
    if BRAIN is None:
        raise SystemExit("No AI set up. Set ONE of ANTHROPIC_API_KEY, OPENAI_API_KEY, GEMINI_API_KEY, "
                         "LLM_BASE_URL, OLLAMA_MODEL or LLM_COMMAND (see the top of bot.py).")
    if BRAIN not in ("command", "ollama", "claude", *OPENAI_STYLE):
        raise SystemExit(f"Unknown LLM={BRAIN}. Use claude, openai, gemini, custom, ollama or command.")
    if BRAIN in OPENAI_STYLE and not MODEL:
        key_var, base = OPENAI_STYLE[BRAIN]
        try:
            r = requests.get(base.rstrip("/") + "/models", timeout=20,
                             headers={"Authorization": "Bearer " + os.environ.get(key_var, "")})
            names = sorted(m["id"].split("/")[-1] for m in r.json()["data"])
        except Exception:
            names = []
        raise SystemExit("Set MODEL to the model your bot should use"
                         + (", for example one of: " + ", ".join(names[:40]) if names else "."))
    print("AI:", BRAIN, "|", os.environ.get("LLM_COMMAND") or OLLAMA_MODEL or MODEL or "claude-sonnet-5-5")


def decide(event):
    text = ask_llm(BRIEFING + "\n\n# Your strategy\n" + STRATEGY,
                   "Event:\n" + json.dumps(event, indent=1) +
                   "\n\nAnswer with JSON only, in this format:\n" + FORMAT[event["type"]])
    return json.JSONDecoder().raw_decode(text[text.find("{"):])[0]   # first JSON object, ignore the rest


def worker(done):
    while not done.is_set():
        try:
            event = requests.get(f"{SERVER}/api/next", headers=HEADERS, timeout=40).json()
        except (requests.RequestException, ValueError) as err:
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
            result = requests.post(f"{SERVER}/api/act", headers=HEADERS, json=answer, timeout=20).json()
        except (requests.RequestException, ValueError) as err:
            result = {"error": str(err)}
        print(kind, "->", answer.get("action", "market"), result)


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
