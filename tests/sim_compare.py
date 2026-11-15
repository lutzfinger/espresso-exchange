"""Does version 2 keep the market moving? The same 12 scripted brains play a 25-minute game (fake clock) under
the classic rules and under version 2. Prints deals (and house sales) per 5-minute block for both.

Run:  .venv/bin/python tests/sim_compare.py [--seeds 3]
"""
from __future__ import annotations

import argparse
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from conftest import FakeClock, make_market  # noqa: E402
from server.house_bots import Brain  # noqa: E402

STYLES = ["Fair Trader", "Hardliner", "Wildcard", "Opener"]


def play(v2: bool, seed: int, teams: int = 12, **cfg) -> list[int]:
    clock = FakeClock()
    m = make_market(clock, n_teams=teams, house=False, seed=seed, v2_rules=v2, require_members=v2, **cfg)
    m.start_game()
    brains = {a.id: Brain(STYLES[i % 4], random.Random(seed * 100 + i)) for i, a in enumerate(m.agents.values())}
    rng = random.Random(seed)
    start = clock()
    while m.mode == "game":
        clock.advance(rng.uniform(0.3, 2.5))
        m.tick()
        for a in list(m.agents.values()):
            ev = m.pop_event(a.id)
            if ev and ev["type"] in ("market", "your_turn"):
                ans = brains[a.id].decide(ev)
                ans["event_id"] = ev["event_id"]
                m.act(a.id, ans)
    blocks = [0] * 5
    for d in m.deals:
        blocks[min(4, int((d["at"] - start) // 300))] += 1
    return blocks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--teams", type=int, default=12)
    ap.add_argument("--every", type=float, default=None, help="house_order_every_s")
    ap.add_argument("--qty", type=int, default=None, help="house_order_qty")
    args = ap.parse_args()
    cfg = {k: v for k, v in (("house_order_every_s", args.every), ("house_order_qty", args.qty)) if v is not None}
    tot = {"classic": [0] * 5, "v2": [0] * 5}
    for s in range(1, args.seeds + 1):
        for name, v2 in (("classic", False), ("v2", True)):
            b = play(v2, s, args.teams, **(cfg if v2 else {}))
            tot[name] = [x + y for x, y in zip(tot[name], b)]
            print(f"seed {s} {name:8s} deals per 5 min: {b}")
    print()
    for name, b in tot.items():
        ratio = b[-1] / b[0] if b[0] else 0
        print(f"TOTAL {name:8s} {b}   last block / first block = {ratio:.0%}")
    ok = tot["v2"][-1] >= 0.5 * tot["v2"][0] and (tot["classic"][-1] < 0.5 * tot["classic"][0])
    print("PASS" if ok else "CHECK", "(v2 keeps >= 50% of its opening activity in the last 5 minutes; classic falls below)")


if __name__ == "__main__":
    main()
