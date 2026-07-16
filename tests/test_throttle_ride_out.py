"""Behavioural tests for throttle ride-out and stale accounting (runner.py).

These stub the network layers (session/search/downloader) and shrink the
cooldown timings so the ride-out logic runs instantly. They assert the three
guarantees of the v0.4.0 change:

  1. a throttle streak is *ridden out* (cooldown + session refresh + retry) and
     the run resumes instead of aborting;
  2. if the throttle never lifts, the run still aborts after MAX_THROTTLE_COOLDOWNS;
  3. throttle empties never age a failures.json entry toward "stale" — only
     genuine per-document failures do.

Run: uv run python -m pytest tests/ -q   (or: uv run python tests/test_throttle_ride_out.py)
"""

from __future__ import annotations

import json
from pathlib import Path

from src import config, runner
from src.models import JudgmentRecord


def _rec(path: str) -> JudgmentRecord:
    return JudgmentRecord(
        path=path, val="v", citation_year="2026", nc_display="nc",
        raw_html="", language_codes=[""],
    )


class _FakeSession:
    """Records how many times the session was (re)initialised."""

    def __init__(self, *_, **__):
        self.inits = 0

    def init(self):
        self.inits += 1


class _ScriptedDownloader:
    """Returns outcomes from a scripted queue; the last entry repeats."""

    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls = 0

    def download(self, _record):
        self.calls += 1
        outcome = self._outcomes[min(self.calls - 1, len(self._outcomes) - 1)]
        return outcome  # (pdf_bytes_or_None, reason_or_None)


def _install(monkeypatch, tmp_path, downloader, records):
    """Wire fakes into runner and shrink cooldowns; return the fake session."""
    session = _FakeSession()
    monkeypatch.setattr(runner, "SCSession", lambda *a, **k: session)
    monkeypatch.setattr(runner, "Downloader", lambda _s: downloader)
    monkeypatch.setattr(runner, "search_judgments", lambda _s, _t: iter(records))
    # Make throttle handling fast + easy to trigger.
    monkeypatch.setattr(runner.time, "sleep", lambda _s: None)
    monkeypatch.setattr(config, "CONSECUTIVE_EMPTY_THRESHOLD", 2)
    monkeypatch.setattr(config, "MAX_THROTTLE_COOLDOWNS", 3)
    monkeypatch.setattr(config, "NO_CAPTCHA_BATCH_SIZE", 1000)
    return session


EMPTY = (None, "empty_pdf")
OK = (b"%PDF-", None)


def test_rides_out_throttle_and_resumes(monkeypatch, tmp_path):
    records = [_rec("2026_1_1_1"), _rec("2026_1_2_2"), _rec("2026_1_3_3")]
    # rec1 empty (streak=1); rec2 empty -> ride-out: empty, empty, then OK on the
    # 3rd cooldown; rec3 OK straight away.
    dl = _ScriptedDownloader([EMPTY, EMPTY, EMPTY, EMPTY, OK, OK])
    session = _install(monkeypatch, tmp_path, dl, records)

    stats = runner.run_scrape("2026-01-01", "2026-01-05", 30, tmp_path)

    assert stats.aborted is False, "should ride out, not abort"
    assert stats.downloaded == 2, stats.summary()   # rec2 (recovered) + rec3
    assert session.inits >= 2, "session refreshed during cooldowns"


def test_aborts_only_after_all_cooldowns(monkeypatch, tmp_path):
    records = [_rec("2026_1_1_1"), _rec("2026_1_2_2"), _rec("2026_1_3_3")]
    dl = _ScriptedDownloader([EMPTY])  # never recovers
    _install(monkeypatch, tmp_path, dl, records)

    stats = runner.run_scrape("2026-01-01", "2026-01-05", 30, tmp_path)

    assert stats.aborted is True, "a true hard block must still abort"


def test_throttle_empties_do_not_mark_stale(monkeypatch, tmp_path):
    # A failures.json entry that keeps coming back empty (throttle) must NOT be
    # aged toward stale, and its retry_count must stay 0.
    entry = {
        "path": "2026_9_9_9", "val": "v", "citation_year": "2026",
        "nc_display": "nc", "language_codes": [""],
        "search_from_date": "2026-01-01", "search_to_date": "2026-01-30",
        "reason": "empty_pdf", "failed_at": "t", "first_seen": "t",
        "retry_count": 4, "status": "active",  # one genuine failure away from stale
    }
    (tmp_path / config.FAILURES_FILENAME).write_text(json.dumps([entry]))

    dl = _ScriptedDownloader([EMPTY])  # always throttled
    _install(monkeypatch, tmp_path, dl, [])

    stats = runner.retry_failures(tmp_path)

    persisted = json.loads((tmp_path / config.FAILURES_FILENAME).read_text())
    assert len(persisted) == 1
    assert persisted[0]["retry_count"] == 4, "throttle must not bump retry_count"
    assert persisted[0]["status"] == "active", "throttle must not mark stale"
    assert stats.stale == 0


def test_genuine_failure_does_mark_stale(monkeypatch, tmp_path):
    entry = {
        "path": "2026_9_9_9", "val": "v", "citation_year": "2026",
        "nc_display": "nc", "language_codes": [""],
        "search_from_date": "2026-01-01", "search_to_date": "2026-01-30",
        "reason": "http_404", "failed_at": "t", "first_seen": "t",
        "retry_count": 4, "status": "active",
    }
    (tmp_path / config.FAILURES_FILENAME).write_text(json.dumps([entry]))

    dl = _ScriptedDownloader([(None, "http_404")])  # genuine per-doc failure
    _install(monkeypatch, tmp_path, dl, [])

    stats = runner.retry_failures(tmp_path)

    persisted = json.loads((tmp_path / config.FAILURES_FILENAME).read_text())
    assert persisted[0]["retry_count"] == 5, "genuine failure ages retry_count"
    assert persisted[0]["status"] == "stale"
    assert stats.stale == 1


if __name__ == "__main__":
    # Minimal runner so the file works without pytest installed.
    import contextlib, tempfile

    class _MP:
        def __init__(self): self._undo = []
        def setattr(self, obj, name, val):
            old = getattr(obj, name); self._undo.append((obj, name, old))
            setattr(obj, name, val)
        def undo(self):
            for obj, name, old in reversed(self._undo): setattr(obj, name, old)

    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        mp = _MP()
        with tempfile.TemporaryDirectory() as d:
            try:
                t(mp, Path(d)); print(f"PASS {t.__name__}")
            except AssertionError as e:
                failed += 1; print(f"FAIL {t.__name__}: {e}")
            except Exception as e:
                failed += 1; print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
            finally:
                mp.undo()
    raise SystemExit(1 if failed else 0)
