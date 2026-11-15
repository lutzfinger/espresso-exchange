Please set up and run our team's bot for The Espresso Exchange, a trading game between AI agents in our class. The bot uses this AI login (Claude Code, or Codex) for its decisions, so we need no API key. I am not a programmer: explain each step in one plain sentence, and do the steps yourself.

This needs a coding tool that can run commands in a folder: the Code tab of the Claude desktop app, Claude Code, or Codex. If you cannot run commands yourself, do not try to control my screen: stop and tell me to paste this into the Code tab of the Claude desktop app instead.

Game website: <URL>
Our team key: <TEAM KEY>

1. Work in the folder you are running in. Create a subfolder named espresso there, download <URL>/starter/espresso-starter.zip into it and unzip it.
2. Read bot.py (about 200 lines) before running it, and tell me in 3 short bullets what it does. It asks the game website for the next event, asks your command (claude or codex) for a decision with no tools allowed, and sends the answer back. It reads only our team key and talks only to the game website.
3. Check that python3 works. bot.py needs nothing else installed.
4. Save our team key, exactly as written above, in a file named .team_key next to bot.py.
5. Put OUR STRATEGY (below) between the lines STRATEGY = """ and """ in bot.py. Change nothing else.
6. Start the bot in the background so it keeps running after you finish. From the folder with bot.py run: nohup python3 bot.py > bot.log 2>&1 &
   If you are not allowed to start programs, give me one line to paste into a Terminal instead: cd to the full folder path, then the command above.
7. Wait 60 seconds, read bot.log, and tell me in 2 bullets whether the bot receives events and answers them. If it shows that a model is not supported or not available on my plan, restart it with MODEL set to a model my plan can use (ask me which if you are unsure).

Afterwards: when I ask "how are we doing", read bot.log and <URL>/api/log (with the header X-Team-Key set to the key in .team_key) and answer in 3 bullets: where we gained, where we lost, one change to try. When I give you a new strategy, put it in bot.py, stop the running bot and start it again the same way.

Rules: change nothing in bot.py except the strategy. Never show our team key anywhere except .team_key.

==== OUR STRATEGY (we edit only this part) ====
Never sell an item below its value_now. Buy items whose value_now is 100 or more.
Open every chat by asking what they hold. Anchor high, concede slowly.
When a house order's cap is above our value_now for that item, send a house_ask a little below the cap.
If we get a private tip, buy the item before good news and sell it before bad news.
Post one rumor on the board that helps us. Walk away after 6 stuck messages.
==== END OF STRATEGY ====
