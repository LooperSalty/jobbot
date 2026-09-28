"""outreach._brave: Brave Search API calls under a monthly budget (paid plans bill every query)."""

from __future__ import annotations

import dataclasses
from datetime import timedelta

from jobbot import clock
from jobbot.outreach import _brave


def test_the_monthly_budget_counts_every_query_and_stops_at_the_cap(repo):
    engine = _brave.BraveEngine(api_key="k", repo=repo, monthly_cap=2)
    assert [engine.take() for _ in range(3)] == [True, True, False]
    assert _brave.used_this_month(repo) == 2


def test_the_budget_starts_again_every_month(repo, monkeypatch):
    engine = _brave.BraveEngine(api_key="k", repo=repo, monthly_cap=1)
    assert engine.take() and not engine.take()
    later = clock.now() + timedelta(days=32)
    monkeypatch.setattr(clock, "now", lambda: later)
    assert engine.take()


def test_a_zero_budget_never_calls_brave(repo):
    assert not _brave.BraveEngine(api_key="k", repo=repo, monthly_cap=0).take()


def test_brave_for_needs_a_key_and_reads_the_setting(ctx, monkeypatch):
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    keyless = dataclasses.replace(ctx, secrets=ctx.secrets.model_copy(update={"brave_api_key": None}))
    assert _brave.brave_for(keyless) is None
    keyed = dataclasses.replace(ctx, secrets=ctx.secrets.model_copy(update={"brave_api_key": "k"}))
    engine = _brave.brave_for(keyed)
    assert (engine.api_key, engine.monthly_cap) == ("k", ctx.settings.night.max_brave_queries)
    assert ctx.settings.night.max_brave_queries > 0
