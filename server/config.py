"""Game parameters. Every number is a default the host can change in the panel."""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields


@dataclass
class Config:
    game_minutes: float = 25.0
    last_call_minutes: float = 22.0
    starting_wealth: int = 1500
    reply_timeout_s: float = 90.0
    message_max_chars: int = 300
    messages_per_chat: int = 20
    posts_per_minute: int = 3
    post_max_chars: int = 200
    market_interval_s: float = 30.0
    practice_reset_minutes: float = 10.0
    rate_limit_per_s: float = 10.0
    redeliver_after_s: float = 45.0
    max_teams: int = 40
    board_in_events: int = 20
    ledger_in_events: int = 15
    house_in_game: bool = False
    public_chats: bool = False  # True: anyone can read chat words live; False: only the host's screen
    house_speed: float = 1.0

    # (min, max) bounds for host edits
    _BOUNDS = {
        "game_minutes": (0.5, 180), "last_call_minutes": (0.1, 180), "starting_wealth": (100, 100000),
        "reply_timeout_s": (5, 600), "message_max_chars": (20, 2000), "messages_per_chat": (2, 200),
        "posts_per_minute": (0, 60), "post_max_chars": (20, 1000), "market_interval_s": (2, 600),
        "practice_reset_minutes": (0.5, 600), "rate_limit_per_s": (1, 100), "redeliver_after_s": (5, 600),
        "max_teams": (1, 200), "board_in_events": (0, 200), "ledger_in_events": (0, 200), "house_speed": (0.05, 10),
    }

    def to_dict(self) -> dict:
        return asdict(self)

    def update(self, changes: dict) -> list[str]:
        """Apply host edits; returns list of errors for rejected keys."""
        errors = []
        names = {f.name: f for f in fields(self)}
        for k, v in (changes or {}).items():
            if k not in names:
                errors.append(f"unknown parameter '{k}'")
                continue
            cur = getattr(self, k)
            try:
                if isinstance(cur, bool):
                    nv = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on")
                elif isinstance(cur, int):
                    nv = int(float(v))
                else:
                    nv = float(v)
            except (TypeError, ValueError):
                errors.append(f"'{k}' must be a number")
                continue
            lo, hi = self._BOUNDS.get(k, (None, None))
            if lo is not None and not (lo <= nv <= hi):
                errors.append(f"'{k}' must be between {lo} and {hi}")
                continue
            setattr(self, k, nv)
        return errors
