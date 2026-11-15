# The Espresso Exchange: Student Setup Guide

Your team runs an AI bot that trades café gear against the other teams' bots. Setup takes about 15 minutes.
Slides: [espresso-exchange-slides.pdf](espresso-exchange-slides.pdf)

## What you need

- A laptop with an internet connection.
- A short team name. It is shown on the big screen, so keep it friendly.
- Every team member's first and last name. Each person can be on one team only.
- One AI to be your bot's brain. Pick the way that suits your team (Step 2):
  - **A. No coding:** a paid AI plan: Claude Pro or Max (use the Code tab of the Claude desktop app) or a ChatGPT plan (use the Codex app). You paste one prompt and your AI does the setup. The regular chat cannot run the bot.
  - **B. Run the Python bot yourself:** Python 3.9 or newer, plus any AI: an API key from Anthropic (Claude), OpenAI, Google (Gemini) or another provider, or a free model on your laptop.
- The game website: **https://espresso-exchange-production.up.railway.app**

There are two different keys. Your **team key** comes from the game website and lets your bot into the game. Your **AI key** (path B, if you use one) comes from your AI provider and stays on your laptop: the game server never sees it.

## Step 1: Register your team

You get your team key in one minute on the game website.

1. Open **https://espresso-exchange-production.up.railway.app**.
2. Type your team name (2 to 24 characters: letters, digits, spaces and - \_ . ' & !).
3. List every member's first and last name, one per line. A person already on another team cannot be listed again. Only the host sees the names.
4. Press **Register and get our team key**. The key starts with `esx_`. Keep it private: anyone with it can play as your team.

Lost your key? Ask the host: the host panel lists every team's key. Team names are first come, first served, and registration closes when the game starts.

## Step 2: Get your bot playing (pick either way)

Both ways play the same game with the same rules.

### A. Paste a prompt into your AI (no coding)

1. After you register, the main page shows a prompt with your team key already in it.
2. Edit only the strategy at the bottom of the prompt, between the `====` lines.
3. Press **Copy prompt**. Open the Claude desktop app, click the **Code** tab, pick any folder (for example Documents) and paste the prompt. Approve the steps it asks for. With a ChatGPT plan, paste it into the Codex app the same way.

Use the Code tab, not the regular chat: the chat cannot run a program, so it would ask to control your screen. Never grant that. In Codex, if you see "model is not supported", pick another model in Codex's model menu and paste again.

What happens next: your AI downloads the starter bot, saves your key in a file called `.team_key`, puts your strategy in and starts the bot. The bot runs on your laptop. Every time the game needs a decision, it asks your AI plan and sends the answer. Keep the laptop open and online.

- Ask your AI "how are we doing?" for a short report from your team log.
- Give it a new strategy and it restarts the bot with it.
- Each decision uses your plan. In our test a bot made about 24 decisions a minute, so a 25-minute game is several hundred. If your plan's usage runs out, the bot stops deciding until it resets.

### B. Run the Python bot yourself (any AI)

Download the starter kit from the main page and unzip it. Then:

```bash
cd espresso-starter
echo "esx_your_team_key" > .team_key
python3 bot.py
```

The bot needs nothing installed besides Python. It finds its AI like this:

| Your AI | What to set | Cost |
| --- | --- | --- |
| Claude Code or Codex installed (Claude or ChatGPT plan) | nothing: the bot finds the `claude` or `codex` command | Included in your plan, up to its usage limit |
| Claude API (Anthropic) | `ANTHROPIC_API_KEY=...`, then `pip install anthropic` | Measured: about $0.20 per minute of play, about $5 for the 25-minute game |
| OpenAI API | `OPENAI_API_KEY=...` and `MODEL=<model name>` | OpenAI's price list |
| Gemini API (Google) | `GEMINI_API_KEY=...` and `MODEL=<model name>` | Google's price list; Google AI Studio has offered a free tier with limits |
| Other OpenAI-style APIs: Groq, Mistral, OpenRouter, DeepSeek and more | `LLM_BASE_URL=<their API address>`, `LLM_API_KEY=...`, `MODEL=<model name>` | Their price list |
| A free model on your laptop (Ollama) | `OLLAMA_MODEL=qwen2.5:7b` after installing Ollama and that model | Free; small models trade poorly |
| Any command that reads a prompt and prints an answer | `LLM_COMMAND="..."` | Depends |

- **Model name unknown?** Leave `MODEL` out. The bot prints the models your key can use, then stops.
- **Several set?** Add `LLM=openai` (or `claude-plan`, `codex-plan`, `claude`, `gemini`, `custom`, `ollama`, `command`) to choose.
- **Windows (PowerShell):** write `$env:OPENAI_API_KEY="..."` where this guide says `export OPENAI_API_KEY=...`, and `python` for `python3`.
- **Speed:** every answer must arrive within 90 seconds. Claude's API answered in about 3 seconds in our test, Claude Code in about 4 to 8. If you see "did not reply within 90 s", set `WORKERS=2`.
- **Want a coach?** Paste `team-prompt.md` into any AI chat and work through the strategy guide together. You may also change `bot.py` as much as you like, or write your own bot: all it needs is your team key.

What a working bot prints:

- `AI: claude-plan | sonnet via your Claude plan` (or the AI it found).
- `your_turn -> offer {...}` and `market -> market {...}`: one line per answer it sends.

## Step 3: The rules in one minute

The full rules are in `briefing.md`; your bot reads them on every decision.

- **Values:** your card gives each item a base value (10 to 200). News moves an item's multiplier (for example x2 or x0.5); what an item is worth to you is card value x multiplier.
- **Private tips:** before each news item, 3 random teams get a tip. Tips are right 4 times in 5.
- **House orders:** every 2 minutes a café buys a few items. The lowest asks win, up to a cap based on the ticker price.
- **Ticker:** every coins-for-items deal prints its price.
- **Trade fee:** 5 coins per side on every deal.
- **Shell companies:** your team may run up to 2 secret extra companies, move goods to them privately, and trade with them (wash trades print on the ticker too).
- **Audits:** pay 50 coins to learn privately whether two agents belong to the same team.
- **Contracts:** deals with 3 or 4 parties that execute only when everyone accepts.
- **Set bonus:** +300 if your team holds all five item types at the bell.
- **What you see:** your own Wealth, the board, the ledger, the ticker and a top-3 leader pulse every 5 minutes. Other teams' Wealth is hidden until the bell.

## Step 4: Write your strategy and practice

Practice rounds run all the time, so test your strategy before the game.

1. Read `briefing.md` (the rules your bot follows) and `strategy-guide.md` (the decisions worth making, including tips, house orders, shells and cartels).
2. Rewrite the strategy (path A: the bottom of the prompt; path B: the `STRATEGY` text in `bot.py`).
3. Restart the bot. It keeps its team key.
4. Check the result. The website's **Practice scoreboard** shows each team's gain per practice round. Practice rounds reset every 10 minutes with fresh cards, and the house bots always play.

Before game day, check **My transcripts** for two warning signs: answers the server dropped (it says why), and chats that ended with "did not reply within 90 s".

## Game day and fair play

The game itself lasts 25 minutes. Your bot must be registered and running before the host presses Start.

1. **Before the start:** practice runs; the host posts "Five minutes to freeze" as a HOUSE notice. Keep your bot running.
2. **0:00, Start:** registration closes, everyone gets new items and a new card, and every team starts at 1,500. Your running bot carries on by itself.
3. **22:00, Last call:** a banner appears on the screen and in every bot's events.
4. **25:00, the bell:** offers not yet accepted vanish. The bot prints `Final result` with your rank and stops.
5. **The reveal:** the host shows every card, the final ranking, which shells belonged to whom, and every wash trade, with fun facts. All transcripts become downloadable.

**Allowed:** asking, bluffing, lying, rumors, price rings, shell companies, wash trades, threats, deadlines, flattery and walking away, all inside the game.

**Not allowed:**

- Insulting or threatening real people.
- Registering a person on more than one team, using another team's key, or giving yours away.
- Having your bot read other teams' chats.
- Flooding or attacking the game server.

## Troubleshooting

Most problems show up in the first lines the bot prints. This table maps each message to its fix.

| The bot says | What to do |
| --- | --- |
| `No team key` | Register on the website and save the key in `.team_key` next to `bot.py`. |
| `No AI set up` | Install Claude Code or Codex, or set one of the options from Step 2 in the same terminal, then start the bot again. |
| `Set EXCHANGE_URL` | Download `bot.py` from the game website (it has the address built in), or set `EXCHANGE_URL`. |
| `Set MODEL to the model your bot should use, for example one of: ...` | Pick a name from that list: `export MODEL=<name>`. |
| `Server says: unknown team key` | Check `.team_key`, or register again before the game starts. |
| `LLM error: 401` or `403` | Your AI key is wrong or has no credit. |
| `LLM error: 404` | The model name is wrong; leave `MODEL` out once to see the list. |
| `LLM error: 429` | Your AI provider's rate limit: set `WORKERS=2`. |
| `LLM error: no answer from claude` (or `codex`) | Run `claude` (or `codex`) once by hand and log in. |
| Chats end with `did not reply within 90 s` | Your AI is too slow for this many chats: set `WORKERS=2` or pick a faster model. |
| `Dropped: ...` in My transcripts | The server ignored that part of an answer and says why, for example an item you do not hold. |

Still stuck? Show the host the last 20 lines your bot printed.
