"""The billing card's state table: one row per reachable observation."""
from __future__ import annotations

from datetime import date

import pytest

from src.billing_state import (
    BillingState,
    BillingStatus,
    DayOutcome,
    Observation,
    Outcome,
    UnavailableKind,
    derive,
)

D1, D2, D3 = date(2026, 11, 8), date(2026, 11, 9), date(2026, 11, 10)
OK = Outcome(ok=True)
REFUSED = Outcome(ok=False, status=403, detail="HTTP 403")
NOT_FOUND = Outcome(ok=False, status=404, detail="HTTP 404")
DOWN = Outcome(ok=False, detail="ConnectError: unreachable")
BROKEN = Outcome(ok=False, status=500, detail="HTTP 500")


def _obs(username=OK, *outcomes, **kw):
    days = tuple(DayOutcome(d, o) for d, o in zip((D1, D2, D3), outcomes))
    return Observation(pat_present=True, username=username, days=days, **kw)


TABLE = [
    ("no token", Observation(), BillingState.NOT_CONFIGURED, None),
    ("no http client", Observation(pat_present=True, httpx_present=False),
     BillingState.UNAVAILABLE, UnavailableKind.CLIENT_MISSING),
    ("not queried yet", Observation(pat_present=True), BillingState.UNKNOWN, None),
    ("token rejected on /user", _obs(Outcome(ok=False, status=401, detail="HTTP 401")),
     BillingState.UNAVAILABLE, UnavailableKind.REJECTED),
    ("/user is 404", _obs(Outcome(ok=False, status=404, detail="HTTP 404")),
     BillingState.UNAVAILABLE, UnavailableKind.USER_LOOKUP_FAILED),
    ("/user body has no login", _obs(Outcome(ok=False, detail="no login", malformed=True)),
     BillingState.UNAVAILABLE, UnavailableKind.USER_LOOKUP_FAILED),
    ("/user network error", _obs(DOWN), BillingState.UNKNOWN, None),
    ("/user server error", _obs(BROKEN), BillingState.UNKNOWN, None),
    ("no days requested", _obs(OK), BillingState.UNKNOWN, None),
    ("every day answered", _obs(OK, OK, OK, OK), BillingState.READY, None),
    ("a day answers 404", _obs(OK, OK, NOT_FOUND),
     BillingState.UNAVAILABLE, UnavailableKind.NOT_ON_ENHANCED_BILLING),
    ("all days refused (403)", _obs(OK, REFUSED, REFUSED, REFUSED),
     BillingState.UNAVAILABLE, UnavailableKind.REJECTED),
    ("all days network error", _obs(OK, DOWN, DOWN, DOWN), BillingState.UNKNOWN, None),
    ("all days server error", _obs(OK, BROKEN, BROKEN, BROKEN), BillingState.UNKNOWN, None),
    ("refused mixed with network error", _obs(OK, REFUSED, DOWN, DOWN),
     BillingState.UNAVAILABLE, UnavailableKind.REJECTED),
    ("one day fails among successes", _obs(OK, OK, DOWN, OK), BillingState.PARTIAL, None),
    ("one day succeeds among refusals", _obs(OK, REFUSED, OK, REFUSED), BillingState.PARTIAL, None),
    ("unexpected exception", Observation(internal_error="boom"), BillingState.UNKNOWN, None),
]


@pytest.mark.parametrize("obs,state,kind", [row[1:] for row in TABLE], ids=[r[0] for r in TABLE])
def test_derive_table(obs, state, kind):
    status = derive(obs)
    assert status.state is state
    assert status.kind is kind
    assert status.available is (state in (BillingState.READY, BillingState.PARTIAL))
    if state is BillingState.READY:
        assert status.reason is None
    else:
        assert status.reason


def test_partial_names_the_failed_days():
    status = derive(_obs(OK, OK, DOWN, DOWN))
    assert status.state is BillingState.PARTIAL
    assert status.failed_days == (D2.isoformat(), D3.isoformat())
    assert "2 of 3" in status.reason


def test_refused_reason_names_the_cause_not_empty_data():
    reason = derive(_obs(OK, REFUSED, REFUSED)).reason
    assert "403" in reason and "Plan" in reason


# state-machine-proof: billing
def test_billing_refuses_an_impossible_observation():
    """Days cannot be fetched before the username resolved, or after it failed."""
    day = (DayOutcome(D1, OK),)
    with pytest.raises(ValueError):
        derive(Observation(pat_present=True, days=day))
    with pytest.raises(ValueError):
        derive(Observation(pat_present=True, username=DOWN, days=day))
    with pytest.raises(ValueError):
        derive(Observation(pat_present=False, username=OK, days=day))
    with pytest.raises(ValueError):
        derive(Observation(pat_present=True, httpx_present=False, username=OK))


# state-machine-proof: billing
def test_billing_refuses_an_illegal_status():
    """A state cannot carry a field combination it never produces."""
    with pytest.raises(ValueError):
        BillingStatus(BillingState.READY, reason="all fine")
    with pytest.raises(ValueError):
        BillingStatus(BillingState.READY, failed_days=("2026-11-09",))
    with pytest.raises(ValueError):
        BillingStatus(BillingState.PARTIAL, reason="some failed")
    with pytest.raises(ValueError):
        BillingStatus(BillingState.UNKNOWN)
    with pytest.raises(ValueError):
        BillingStatus(BillingState.UNAVAILABLE, reason="no")
    with pytest.raises(ValueError):
        BillingStatus(BillingState.UNKNOWN, reason="?", kind=UnavailableKind.REJECTED)
