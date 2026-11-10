"""The official-billing card's state, declared in one place.

The card has five modes. Every fact that decides between them (token present,
HTTP client present, how the username lookup went, how each requested day went)
goes into one ``Observation``, and ``derive`` is the only code that maps an
observation to a ``BillingState``. The API response, the settings dialog's
"configured" line and the page all read the derived ``BillingStatus``; none of
them re-derive the mode from an env var, a cache or an empty list.

States (see docs/billing-state.md for the table):

* ``NOT_CONFIGURED`` - no token set.
* ``UNAVAILABLE``    - the API answered and the answer is "no": the token was
  rejected, the account is not on the enhanced billing platform, or the
  username lookup failed. Carries a ``kind``.
* ``READY``          - every requested day answered; there may be no spend.
* ``PARTIAL``        - some days answered, some failed; carries the failed dates.
* ``UNKNOWN``        - nothing could be established (network down, server
  errors, nothing asked yet). Never read as "no spend".

Pure and import-light: no network, no clock, no environment.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import List, Optional, Tuple

PAT_ENV = "GITHUB_COPILOT_BILLING_PAT"

_NOT_ON_ENHANCED_BILLING = (
    "GitHub billing API returned 404 (account likely not on the enhanced billing platform)"
)


class BillingState(str, Enum):
    NOT_CONFIGURED = "not_configured"
    UNAVAILABLE = "unavailable"
    READY = "ready"
    PARTIAL = "partial"
    UNKNOWN = "unknown"


class UnavailableKind(str, Enum):
    USER_LOOKUP_FAILED = "user_lookup_failed"
    NOT_ON_ENHANCED_BILLING = "not_on_enhanced_billing"
    REJECTED = "rejected"
    CLIENT_MISSING = "client_missing"


@dataclass(frozen=True)
class Outcome:
    """How one HTTP call went: answered fine, answered with an error, or never answered."""

    ok: bool
    status: Optional[int] = None  # HTTP status when the server answered
    detail: str = ""
    malformed: bool = False  # answered 2xx but the body was not what the API documents


@dataclass(frozen=True)
class DayOutcome:
    day: date
    outcome: Outcome


@dataclass(frozen=True)
class Observation:
    """Every fact the card's state depends on, gathered once per request."""

    pat_present: bool = False
    httpx_present: bool = True
    username: Optional[Outcome] = None  # None = the lookup was not attempted
    days: Tuple[DayOutcome, ...] = ()
    internal_error: Optional[str] = None  # an unexpected exception while gathering


@dataclass(frozen=True)
class BillingStatus:
    state: BillingState
    reason: Optional[str] = None
    failed_days: Tuple[str, ...] = ()
    kind: Optional[UnavailableKind] = None

    def __post_init__(self) -> None:
        # The legal field combinations per state; building any other is a bug.
        if self.state is BillingState.READY:
            legal = self.reason is None and not self.failed_days
        elif self.state is BillingState.PARTIAL:
            legal = bool(self.reason) and bool(self.failed_days)
        else:
            legal = bool(self.reason) and not self.failed_days
        if (self.kind is not None) != (self.state is BillingState.UNAVAILABLE):
            legal = False
        if not legal:
            raise ValueError("illegal billing status: " + repr(self))

    @property
    def available(self) -> bool:
        """Compatibility field for older clients: there are figures to show."""
        return self.state in (BillingState.READY, BillingState.PARTIAL)


def _check(obs: Observation) -> None:
    """Refuse an observation that no real request sequence can produce."""
    if obs.days and obs.username is None:
        raise ValueError("days were fetched before the username was resolved")
    if obs.days and not obs.username.ok:  # type: ignore[union-attr]
        raise ValueError("days were fetched after the username lookup failed")
    if (obs.username is not None or obs.days) and not (obs.pat_present and obs.httpx_present):
        raise ValueError("calls were made without a token or an HTTP client")


def _user_failure(outcome: Outcome) -> BillingStatus:
    if outcome.status in (401, 403):
        return BillingStatus(
            BillingState.UNAVAILABLE,
            reason="GitHub rejected the token (HTTP %d); check %s" % (outcome.status, PAT_ENV),
            kind=UnavailableKind.REJECTED,
        )
    transient = outcome.status is None and not outcome.malformed
    if transient or (outcome.status is not None and outcome.status >= 500):
        return BillingStatus(
            BillingState.UNKNOWN,
            reason="could not reach GitHub to look up the username: " + outcome.detail,
        )
    why = ("HTTP %d" % outcome.status) if outcome.status is not None else outcome.detail
    return BillingStatus(
        BillingState.UNAVAILABLE,
        reason="could not resolve GitHub username: " + why,
        kind=UnavailableKind.USER_LOOKUP_FAILED,
    )


def derive(obs: Observation) -> BillingStatus:
    """The one mapping from an observation to the card's state."""
    if obs.internal_error is not None:
        return BillingStatus(BillingState.UNKNOWN, reason=obs.internal_error)
    _check(obs)
    if not obs.pat_present:
        return BillingStatus(
            BillingState.NOT_CONFIGURED,
            reason="no PAT configured — set " + PAT_ENV + " in .env to enable",
        )
    if not obs.httpx_present:
        return BillingStatus(
            BillingState.UNAVAILABLE,
            reason="httpx not installed",
            kind=UnavailableKind.CLIENT_MISSING,
        )
    if obs.username is None:
        return BillingStatus(BillingState.UNKNOWN, reason="billing has not been queried yet")
    if not obs.username.ok:
        return _user_failure(obs.username)
    if not obs.days:
        return BillingStatus(BillingState.UNKNOWN, reason="no days were requested")

    failed = [d for d in obs.days if not d.outcome.ok]
    if any(d.outcome.status == 404 for d in failed):
        return BillingStatus(
            BillingState.UNAVAILABLE,
            reason=_NOT_ON_ENHANCED_BILLING,
            kind=UnavailableKind.NOT_ON_ENHANCED_BILLING,
        )
    if not failed:
        return BillingStatus(BillingState.READY)
    if len(failed) < len(obs.days):
        names: List[str] = sorted(d.day.isoformat() for d in failed)
        return BillingStatus(
            BillingState.PARTIAL,
            reason="%d of %d days could not be fetched" % (len(failed), len(obs.days)),
            failed_days=tuple(names),
        )
    refused = [d.outcome.status for d in failed if d.outcome.status in (401, 403)]
    if refused:
        return BillingStatus(
            BillingState.UNAVAILABLE,
            reason=('GitHub refused the billing request (HTTP %d); the token needs the '
                    '"Plan" read-only permission' % refused[0]),
            kind=UnavailableKind.REJECTED,
        )
    return BillingStatus(
        BillingState.UNKNOWN,
        reason="could not fetch billing data: " + (failed[0].outcome.detail or "no answer"),
    )
