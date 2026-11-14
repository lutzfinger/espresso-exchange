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


def make_market(clock, n_teams=0, house=True, seed=7, store=None, **cfg):
    m = Market(Config(**cfg), store=store or Store(":memory:"), clock=clock, rng=random.Random(seed))
    if house:
        m.ensure_house_bots()
    for i in range(n_teams):
        m.register(f"Team {i + 1}")
    m.start_round("practice")
    return m


@pytest.fixture
def market(clock):
    return make_market(clock, n_teams=3)
