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
    # version 2 rules; v2_rules=False switches the whole game back to the classic rules
    v2_rules: bool = True
    require_members: bool = True   # teams register with every member's full name; one team per person
    max_members: int = 8
    news_count: int = 4            # news shocks per game (practice rounds get a share, at least one)
    tip_lead_s: float = 120.0      # private tips go out this long before each news item
    tips_per_news: int = 3
    tip_accuracy: float = 0.8      # chance that a tip is right
    house_order_first_s: float = 90.0
    house_order_every_s: float = 120.0   # 0 = no house orders
    house_order_window_s: float = 60.0
    house_order_qty: int = 3       # for 25+ teams, 4 keeps more trades going
    trade_fee: int = 5             # coins per side per deal
    audit_cost: int = 50
    shells_per_team: int = 2
    set_bonus: int = 300           # hold all five item types at the bell
    pulse_every_s: float = 300.0   # leader pulse on the board; 0 = off
    public_wealth: bool = False    # True: every agent's Wealth is public during play (classic)
    contract_window_s: float = 60.0

    # (min, max) bounds for host edits
    _BOUNDS = {
        "game_minutes": (0.5, 180), "last_call_minutes": (0.1, 180), "starting_wealth": (100, 100000),
        "reply_timeout_s": (5, 600), "message_max_chars": (20, 2000), "messages_per_chat": (2, 200),
        "posts_per_minute": (0, 60), "post_max_chars": (20, 1000), "market_interval_s": (2, 600),
        "practice_reset_minutes": (0.5, 600), "rate_limit_per_s": (1, 100), "redeliver_after_s": (5, 600),
        "max_teams": (1, 200), "board_in_events": (0, 200), "ledger_in_events": (0, 200), "house_speed": (0.05, 10),
        "max_members": (1, 20), "news_count": (0, 12), "tip_lead_s": (5, 600), "tips_per_news": (0, 50),
        "tip_accuracy": (0, 1), "house_order_first_s": (0, 3600), "house_order_every_s": (0, 3600),
        "house_order_window_s": (5, 600), "house_order_qty": (1, 10), "trade_fee": (0, 1000),
        "audit_cost": (0, 10000), "shells_per_team": (0, 5), "set_bonus": (0, 10000), "pulse_every_s": (0, 3600),
        "contract_window_s": (5, 600),
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
