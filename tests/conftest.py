import os
import random
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from server.config import Config  # noqa: E402
from server.market import Market  # noqa: E402
from server.store import Store  # noqa: E402


class FakeClock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s
        return self.t


@pytest.fixture
def clock():
    return FakeClock()


def person(i: int) -> str:
    """A fake, unique, valid member name for tests: Pat A Test, Pat B Test, ... Pat Aa Test."""
    s, n = "", i
    while True:
        s = chr(97 + n % 26) + s
        n = n // 26 - 1
        if n < 0:
            break
    return f"Pat {s.title()} Test"


_people = iter(range(10_000_000))


def new_person() -> str:
    return person(next(_people))


def make_market(clock, n_teams=0, house=True, seed=7, store=None, **cfg):
    m = Market(Config(**cfg), store=store or Store(":memory:"), clock=clock, rng=random.Random(seed))
    if house:
        m.ensure_house_bots()
    for i in range(n_teams):
        m.register(f"Team {i + 1}", members=[person(i)])
    m.start_round("practice")
    return m


@pytest.fixture
def market(clock):
    return make_market(clock, n_teams=3)
