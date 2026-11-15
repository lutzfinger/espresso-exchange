"""FastAPI app: bot API, long-poll event queues, house bots, spectator, host panel, starter kit."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import io
import json
import logging
import os
import random
import secrets
import time
import zipfile
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response

from .config import Config
from .house_bots import Brain
from .market import HOUSE_NAMES, GameError, Market
from .store import Store

log = logging.getLogger("exchange")
ROOT = Path(__file__).resolve().parent.parent
STATIC = Path(__file__).resolve().parent / "static"
STARTER = ROOT / "starter"


class Limiter:
    """Token bucket per key: protects the server from runaway bots (not a game rule)."""

    def __init__(self, rate: float, burst: float):
        self.rate, self.burst = rate, burst
        self.buckets: dict[str, list] = {}

    def allow(self, key: str, now: float) -> bool:
        b = self.buckets.get(key)
        if b is None:
            b = self.buckets[key] = [self.burst, now]
        tokens = min(self.burst, b[0] + (now - b[1]) * self.rate)
        b[1] = now
        if tokens < 1:
            b[0] = tokens
            return False
        b[0] = tokens - 1
        return True


MAX_BODY = 64 * 1024  # a bot's answer is a few hundred bytes


class BodyLimit:
    """Refuse request bodies over MAX_BODY, announced or streamed, before the app reads them."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        length = dict(scope.get("headers") or []).get(b"content-length")
        if length is not None and length.isdigit() and int(length) > MAX_BODY:
            body = json.dumps({"type": "error", "error": f"request body over {MAX_BODY // 1024} KB"}).encode()
            await send({"type": "http.response.start", "status": 413,
                        "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
            return await send({"type": "http.response.body", "body": body})
        seen = 0

        async def limited():
            nonlocal seen
            msg = await receive()
            if msg["type"] == "http.request":
                seen += len(msg.get("body", b""))
                if seen > MAX_BODY:  # streamed without a length: the app sees a broken body
                    return {"type": "http.disconnect"}
            return msg

        return await self.app(scope, limited, send)


class Hub:
    """Holds the market and the asyncio plumbing around it."""

    def __init__(self, market: Market, host_key: str, screen_key: str, house: bool = True):
        self.m = market
        self.host_key = host_key
        self.screen_key = screen_key
        self.signals: dict[int, asyncio.Event] = {}
        self.team_limiter = Limiter(market.cfg.rate_limit_per_s, market.cfg.rate_limit_per_s * 2)
        self.ip_limiter = Limiter(60, 120)
        self.register_limiter = Limiter(1.0, 40)  # a classroom shares one public IP
        self.register_all = Limiter(2.0, 60)      # and a cap over all addresses together
        self.host_fail_limiter = Limiter(1.0, 10)  # wrong host or screen keys, per address
        self.polls: dict[int, int] = {}            # open long-polls per team
        self.tasks: list[asyncio.Task] = []
        self.house_enabled = house
        self.started_at = time.time()
        market.on_ready = self._signal

    def _signal(self, aid: int):
        ev = self.signals.get(aid)
        if ev is None:
            ev = self.signals[aid] = asyncio.Event()
        ev.set()

    async def next_event(self, aid: int, wait: float) -> dict:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, min(30.0, wait))
        while True:
            ev = self.m.pop_event(aid)
            if ev is not None:
                return ev
            remaining = deadline - loop.time()
            if remaining <= 0:
                return self.m.wait_event(aid)
            sig = self.signals.setdefault(aid, asyncio.Event())
            sig.clear()
            try:
                await asyncio.wait_for(sig.wait(), remaining)
            except asyncio.TimeoutError:
                pass

    async def ticker(self):
        while True:
            try:
                self.m.tick()
            except Exception:  # never let the clock die
                log.exception("tick failed")
            await asyncio.sleep(0.5)

    async def house_bot(self, name: str):
        brain = Brain(name, random.Random())
        while True:
            try:
                a = self.m.agents.get(self.m.by_name.get(name.lower(), -1))
                if a is None:
                    await asyncio.sleep(2)
                    continue
                ev = await self.next_event(a.id, 30)
                if ev.get("type") in ("your_turn", "market"):
                    delay = random.uniform(1.5, 4.5) * self.m.cfg.house_speed
                    if ev["type"] == "market":
                        delay = random.uniform(0.2, 2.0) * self.m.cfg.house_speed
                    await asyncio.sleep(delay)
                    if ev.get("agents"):  # house bots leave the (secret) shell companies alone
                        ev["agents"] = [n for n in ev["agents"] if self._not_shell(n)]
                    answer = brain.decide(ev)
                    answer["event_id"] = ev["event_id"]
                    self.m.act(a.id, answer)
                elif ev.get("type") == "game_over":
                    await asyncio.sleep(5)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("house bot %s failed", name)
                await asyncio.sleep(2)

    def _not_shell(self, name: str) -> bool:
        a = self.m.agents.get(self.m.by_name.get(name.lower(), -1))
        return a is None or a.owner is None

    def start(self):
        self.tasks.append(asyncio.create_task(self.ticker()))
        if self.house_enabled:
            for n in HOUSE_NAMES:
                self.tasks.append(asyncio.create_task(self.house_bot(n)))

    async def stop(self):
        for t in self.tasks:
            t.cancel()
        for t in self.tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t


def _derive(secret: str, label: str, n: int = 16) -> str:
    return hmac.new(secret.encode(), label.encode(), hashlib.sha256).hexdigest()[:n]


def build_market(db_path: str | None, config: Config | None = None) -> tuple[Market, str, str]:
    store = Store(db_path) if db_path else Store(":memory:")
    secret = os.environ.get("SECRET_KEY") or store.get_meta("secret")
    if not secret:
        secret = secrets.token_hex(24)
        store.set_meta("secret", secret)
    host_key = os.environ.get("HOST_KEY") or store.get_meta("host_key")
    if not host_key:
        host_key = "host-" + _derive(secret, "host", 12)
        store.set_meta("host_key", host_key)
    screen_key = os.environ.get("SCREEN_KEY") or _derive(secret, "screen", 10)
    cfg = config or Config()
    saved_cfg = store.get_meta("config")
    if saved_cfg and config is None:
        cfg.update(json.loads(saved_cfg))
    m = Market(cfg, store=store, secret=secret)
    restored = m.restore()
    m.ensure_house_bots()
    if not restored:
        m.start_round("practice")
    return m, host_key, screen_key


def create_app(db_path: str | None = None, config: Config | None = None, house: bool = True) -> FastAPI:
    db_path = db_path if db_path is not None else os.environ.get("DB_PATH", str(ROOT / "data" / "exchange.db"))
    holder: dict = {}

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        market, host_key, screen_key = build_market(db_path, config)
        hub = Hub(market, host_key, screen_key, house=house)
        holder["hub"] = hub
        app.state.hub = hub
        hub.start()
        log.warning("Espresso Exchange up. Host panel: /host  (host key: %s...)  screen key: %s",
                    host_key[:9], screen_key)
        yield
        await hub.stop()

    app = FastAPI(title="The Espresso Exchange", lifespan=lifespan, docs_url="/api/docs", redoc_url=None)
    app.add_middleware(BodyLimit)
    proxy_hops = int(os.environ.get("PROXY_HOPS", "0"))  # 1 on Railway: its proxy appends the real address

    def hub() -> Hub:
        return holder["hub"]

    def err(status: int, msg: str, **extra) -> JSONResponse:
        return JSONResponse({"type": "error", "error": msg, **extra}, status_code=status)

    def team_key(request: Request) -> str | None:
        return request.headers.get("x-team-key") or request.query_params.get("key")

    def client_ip(request: Request) -> str:
        """The address our own proxy saw. A client can put anything in X-Forwarded-For, but only on the left:
        each trusted proxy appends the address it saw, so count PROXY_HOPS entries from the right."""
        fwd = [x.strip() for x in request.headers.get("x-forwarded-for", "").split(",") if x.strip()]
        if proxy_hops and len(fwd) >= proxy_hops:
            return fwd[-proxy_hops]
        return request.client.host if request.client else "?"

    async def auth_team(request: Request):
        h = hub()
        key = team_key(request)
        a = h.m.agent_for_key(key)
        if a is None or a.is_house:
            await asyncio.sleep(2)  # slow down a bot stuck with a bad key instead of letting it spin
            return None, err(401, "unknown team key: register on the game website (team name plus every member's "
                                  "full name), or POST /api/register {\"team\": \"<name>\", \"members\": "
                                  "[\"Ana Lopez\", \"Ben Wu\"]}, then send the key in the X-Team-Key header")
        h.team_limiter.rate = h.m.cfg.rate_limit_per_s
        h.team_limiter.burst = h.m.cfg.rate_limit_per_s * 2
        if not h.team_limiter.allow(a.key, time.monotonic()):
            await asyncio.sleep(1)
            return None, err(429, f"rate limit: at most {h.m.cfg.rate_limit_per_s:g} requests per second per team "
                                  f"key", retry_after=1)
        a.requests += 1
        return a, None

    def is_host(request: Request) -> bool:
        k = request.headers.get("x-host-key") or request.query_params.get("key") or ""
        return hmac.compare_digest(k, hub().host_key)

    async def host_denied(request: Request) -> JSONResponse:
        """A wrong host key: slow, and rate limited per address, so guessing it is hopeless."""
        if not hub().host_fail_limiter.allow("host:" + client_ip(request), time.monotonic()):
            await asyncio.sleep(2)
            return err(429, "too many wrong host keys from this address")
        await asyncio.sleep(1)
        return err(403, "host key required")

    def base_url(request: Request) -> str:
        proto = request.headers.get("x-forwarded-proto", request.url.scheme)
        host = request.headers.get("x-forwarded-host", request.headers.get("host", request.url.netloc))
        return f"{proto}://{host}"

    # ------------------------------------------------------------------ bot API
    @app.post("/api/register")
    async def register(request: Request):
        h = hub()
        now = time.monotonic()
        if not h.register_limiter.allow(client_ip(request), now) or not h.register_all.allow("all", now):
            return err(429, "too many registrations right now; wait a minute")
        try:
            body = await request.json()
        except Exception:
            body = {}
        body = body if isinstance(body, dict) else {}
        name = body.get("team") or body.get("name") or body.get("agent")
        members = body.get("members", body.get("team_members"))
        try:
            a = h.m.register(name, members=members)
        except GameError as e:
            return err(400, str(e))
        url = base_url(request)
        return {"team": a.name, "key": a.key, "server": url, "members": a.members,
                "next": f"export EXCHANGE_URL={url} TEAM_KEY={a.key}  then  python bot.py",
                "team_page": f"{url}/team#key={a.key}"}

    @app.post("/api/shell")
    async def api_shell(request: Request):
        """Create a shell company for the team (the bot can also send "create_shell" in any answer)."""
        a, e = await auth_team(request)
        if e:
            return e
        try:
            body = await request.json()
        except Exception:
            body = {}
        body = body if isinstance(body, dict) else {}
        try:
            s = hub().m.create_shell(a, body.get("name") or body.get("shell"))
        except GameError as ex:
            return err(400, str(ex))
        return {"shell": s.name, "team": a.name, "active": s.active,
                "note": "It starts empty. Move coins or items to it with a transfer in any answer."}

    @app.get("/api/next")
    async def api_next(request: Request, wait: float = 30):
        a, e = await auth_team(request)
        if e:
            return e
        h = hub()
        if h.polls.get(a.id, 0) >= 12:  # a bot needs a few loops, not hundreds of open connections
            await asyncio.sleep(1)
            return err(429, "too many open /api/next requests for this team (12 at most)", retry_after=1)
        h.polls[a.id] = h.polls.get(a.id, 0) + 1
        try:
            return await h.next_event(a.id, wait)
        finally:
            h.polls[a.id] -= 1

    @app.post("/api/act")
    async def api_act(request: Request):
        a, e = await auth_team(request)
        if e:
            return e
        try:
            body = await request.json()
        except Exception:
            body = None
        try:
            return hub().m.act(a.id, body)
        except Exception:  # never a 500 for a strange answer: say so and carry on
            log.exception("act failed for team %s", a.name)
            return err(400, "the server could not read that answer; check its JSON and try again")

    @app.get("/api/log")
    async def api_log(request: Request):
        a, e = await auth_team(request)
        if e:
            return e
        return hub().m.team_log(a)

    @app.get("/api/board")
    async def api_board(request: Request, since: int = 0):
        h = hub()
        if not h.ip_limiter.allow(client_ip(request), time.monotonic()):
            await asyncio.sleep(0.5)
            return err(429, "too many board reads from this address; slow down a little")
        return h.m.board(since=since)

    def ip_slow(request: Request, what: str):
        return not hub().ip_limiter.allow(what + ":" + client_ip(request), time.monotonic())

    @app.get("/api/status")
    async def api_status(request: Request):
        if ip_slow(request, "status"):
            await asyncio.sleep(0.5)
            return err(429, "too many reads from this address; slow down a little")
        m = hub().m
        return {"mode": m.mode, "clock": m.clock_info(), "registration_open": m.registration_open,
                "agents": [a.name for a in m.active_agents()],
                "teams": len(m.teams()), "house_bots": HOUSE_NAMES}

    @app.get("/api/scoreboard")
    async def api_scoreboard(request: Request):
        if ip_slow(request, "score"):
            await asyncio.sleep(0.5)
            return err(429, "too many reads from this address; slow down a little")
        m = hub().m
        live = {a.name: m.team_score(a.id) - a.start_wealth for a in m.active_agents() if a.owner is None} \
            if m.mode == "practice" else {}
        return {"mode": m.mode, "round": m.round["id"] if m.round else None, "live": live,
                "history": list(m.practice_history)}

    @app.get("/api/dashboard")
    async def api_dashboard(request: Request):
        """Public dashboard for humans and agents: Wealth, deals, board, who chats with whom."""
        h = hub()
        if not h.ip_limiter.allow("dash:" + client_ip(request), time.monotonic()):
            await asyncio.sleep(0.5)
            return err(429, "too many dashboard reads from this address; slow down a little")
        return h.m.dashboard(full=await screen_ok(request))

    async def screen_ok(request: Request) -> bool:
        """The host's screen (screen key) or the host key: the dashboard with every agent's Wealth."""
        h = hub()
        if is_host(request):
            return True
        k = request.headers.get("x-screen-key") or request.query_params.get("screen") or ""
        if k and hmac.compare_digest(k, h.screen_key):
            return True
        if k:
            await host_denied(request)  # slow and rate limited, like a wrong host key
        return False

    async def watch_denied(request: Request):
        """Chat words are for the host's screen during play: the screen key or the host key. Everyone may read
        them after the reveal, or at any time if the host sets public_chats. Agents never: they read only
        their own chats (events and /api/log)."""
        h = hub()
        if request.headers.get("x-team-key") or (request.query_params.get("key") and not is_host(request)):
            return err(403, "agents may read only their own chats: use /api/next and /api/log")
        if ip_slow(request, "watch"):
            await asyncio.sleep(0.5)
            return err(429, "too many reads from this address; slow down a little")
        if h.m.cfg.public_chats or h.m.mode == "reveal" or is_host(request):
            return None
        k = request.headers.get("x-screen-key") or request.query_params.get("screen") or ""
        if k and hmac.compare_digest(k, h.screen_key):
            return None
        if k:
            return await host_denied(request)
        return err(403, "chat words are private until the reveal; the host's screen shows them")

    @app.get("/api/watch/chats")
    async def watch_chats(request: Request, agent: str | None = None):
        e = await watch_denied(request)
        if e:
            return e
        return hub().m.watch_chats(agent)

    @app.get("/api/watch/chat/{chat_id}")
    async def watch_chat(chat_id: int, request: Request):
        e = await watch_denied(request)
        if e:
            return e
        c = hub().m.watch_chat(chat_id)
        return c if c else err(404, "no such chat")

    @app.get("/api/transcripts")
    async def api_transcripts(request: Request, fmt: str = "md"):
        m = hub().m
        if not (is_host(request) or m.mode == "reveal"):
            if request.headers.get("x-host-key") or request.query_params.get("key"):
                return await host_denied(request)
            return err(403, "transcripts open at the reveal")
        if fmt == "json":
            return JSONResponse(m.export(), headers={"Content-Disposition":
                                                     f"attachment; filename=espresso-round-{m.round['id']}.json"})
        return PlainTextResponse(m.transcripts_text(), headers={
            "Content-Disposition": f"attachment; filename=espresso-transcripts-round-{m.round['id']}.md"})

    # ------------------------------------------------------------------ host API
    @app.get("/api/host/state")
    async def host_state(request: Request):
        if not is_host(request):
            return await host_denied(request)
        st = hub().m.host_state()
        st["screen_key"] = hub().screen_key
        st["your_address"] = client_ip(request)  # check that a forged X-Forwarded-For does not change this
        return st

    @app.post("/api/host/{action}")
    async def host_action(action: str, request: Request):
        if not is_host(request):
            return await host_denied(request)
        h = hub()
        m = h.m
        try:
            body = await request.json()
        except Exception:
            body = {}
        body = body or {}
        if action == "start_game":
            m.start_game()
        elif action == "practice":
            m.to_practice()
        elif action == "reset":
            if m.mode != "practice":
                return err(400, "reset only works in practice mode; use 'practice' to go back")
            m.start_round("practice")
        elif action == "bell":
            m.ring_bell()
        elif action == "reveal":
            if not m.reveal():
                return err(400, "reveal works after the bell")
        elif action == "notice":
            m.post_house_notice(body.get("text", ""))
        elif action == "registration":
            m.registration_open = bool(body.get("open", True))
        elif action == "config":
            errors = m.cfg.update(body)
            m.store.set_meta("config", json.dumps(m.cfg.to_dict()))
            if errors:
                return err(400, "; ".join(errors), config=m.cfg.to_dict())
        elif action == "remove":
            if not m.remove_team(body.get("team", "")):
                return err(404, "no such team")
        elif action == "wipe":
            if body.get("confirm") != "WIPE":
                return err(400, 'send {"confirm": "WIPE"} to remove every team')
            m.wipe_teams()
        elif action == "defaults":
            m.cfg.__init__()
            m.store.set_meta("config", json.dumps(m.cfg.to_dict()))
        elif action == "extend":
            if m.mode == "game":
                m.round["ends_at"] += float(body.get("minutes", 1)) * 60
                m.store.update_round_end(m.round["id"], m.round["ends_at"])
        else:
            return err(404, f"unknown host action '{action}'")
        st = m.host_state()
        st["screen_key"] = h.screen_key
        return st

    @app.get("/api/host/export")
    async def host_export(request: Request):
        if not is_host(request):
            return await host_denied(request)
        m = hub().m
        data = m.export()
        data["rounds"] = m.store.rounds(limit=200)
        return JSONResponse(data, headers={"Content-Disposition": "attachment; filename=espresso-export.json"})

    # ------------------------------------------------------------------ starter kit
    def starter_text(name: str, request: Request, team: str = "<TEAM NAME>") -> str:
        text = (STARTER / name).read_text()
        return text.replace("<URL>", base_url(request)).replace("<practice server URL>", base_url(request)) \
            .replace("<TEAM NAME>", team)

    @app.get("/starter/{name}")
    async def starter_file(name: str, request: Request, team: str = "<TEAM NAME>"):
        allowed = {"bot.py", "briefing.md", "strategy-guide.md", "team-prompt.md", "ai-prompt.md"}
        if name == "espresso-starter.zip":
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                for f in sorted(allowed):
                    z.writestr(f"espresso-starter/{f}", starter_text(f, request, team))
            return Response(buf.getvalue(), media_type="application/zip",
                            headers={"Content-Disposition": "attachment; filename=espresso-starter.zip"})
        if name not in allowed:
            return err(404, "no such file")
        return PlainTextResponse(starter_text(name, request, team))

    # ------------------------------------------------------------------ pages
    def page(name: str):
        return FileResponse(STATIC / name, media_type="text/html", headers={"Cache-Control": "no-cache"})

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return page("index.html")

    @app.get("/screen", response_class=HTMLResponse)
    async def screen():
        return page("screen.html")

    @app.get("/host", response_class=HTMLResponse)
    async def host():
        return page("host.html")

    @app.get("/team", response_class=HTMLResponse)
    async def team():
        return page("team.html")

    @app.get("/scoreboard", response_class=HTMLResponse)
    async def scoreboard():
        return page("scoreboard.html")

    @app.get("/static/{name}")
    async def static(name: str):
        p = (STATIC / name).resolve()
        if p.parent != STATIC.resolve() or not p.exists():
            return err(404, "not found")
        return FileResponse(p, headers={"Cache-Control": "no-cache"})

    @app.get("/healthz")
    async def healthz():
        h = hub()
        return {"ok": True, "mode": h.m.mode, "uptime_s": int(time.time() - h.started_at)}

    return app


app = create_app()
