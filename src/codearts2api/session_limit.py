"""Polling policy for the upstream concurrent-session cap.

When the provider explicitly reports its concurrent-session limit
(``is_session_limit_message`` in ``client.py``), the gateway keeps the
request's global slot, releases the account slot, and polls with backoff
until the cap lifts, the client disconnects, the operator cancels, or a
permanent error replaces the limit. There is deliberately no total attempt
or time cap: a capped conversation waits instead of failing.
"""

from __future__ import annotations

import random

# Backoff between upstream polls while the provider cap is in effect. Tests
# may monkeypatch either name, e.g.
# ``monkeypatch.setattr("codearts2api.session_limit.SESSION_LIMIT_MAX_DELAY_SECONDS", 0.01)``.
SESSION_LIMIT_INITIAL_DELAY_SECONDS = 2.0
SESSION_LIMIT_MAX_DELAY_SECONDS = 30.0
SESSION_LIMIT_BACKOFF_MULTIPLIER = 2.0
SESSION_LIMIT_JITTER_RATIO = 0.2


def next_session_limit_delay(attempt: int) -> float:
    """Exponential backoff with jitter for the 0-based poll ``attempt``.

    The result never exceeds ``SESSION_LIMIT_MAX_DELAY_SECONDS``. Growth is
    saturated iteratively instead of ``pow()``: an unbounded exponent
    overflows a float after ~1024 doublings, and past the cap further growth
    is discarded anyway, so the loop also stays cheap for huge attempts.
    """
    base = SESSION_LIMIT_INITIAL_DELAY_SECONDS
    for _ in range(min(max(attempt, 0), 64)):
        if base >= SESSION_LIMIT_MAX_DELAY_SECONDS:
            break
        base *= SESSION_LIMIT_BACKOFF_MULTIPLIER
    base = min(base, SESSION_LIMIT_MAX_DELAY_SECONDS)
    jitter = base * SESSION_LIMIT_JITTER_RATIO
    return min(SESSION_LIMIT_MAX_DELAY_SECONDS, max(0.0, base + random.uniform(-jitter, jitter)))
