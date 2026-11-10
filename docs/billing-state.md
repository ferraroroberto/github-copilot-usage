# Billing card state

The optional "Official GitHub billing" card has five modes. They used to be inferred from an env var, a module-level cache and an empty list, which let a failed probe read as "no spend". The modes are now declared in `src/billing_state.py` (entry `billing` in `.fleet.toml` `[[state_machines]]`) and everything reads them from there.

## States

| State | Meaning | Carries |
|---|---|---|
| `not_configured` | No `GITHUB_COPILOT_BILLING_PAT` set | reason |
| `unavailable` | GitHub answered and the answer is "no" | reason, `reason_kind` |
| `ready` | Every requested day answered; there may be no spend | nothing |
| `partial` | Some days answered, some failed | reason, `failed_days` |
| `unknown` | Nothing could be established (network down, server errors, not queried yet, an unexpected exception) | reason |

`unavailable` has four kinds: `rejected` (401/403 on `/user`, or every day refused: the token lacks the "Plan" read permission), `not_on_enhanced_billing` (404), `user_lookup_failed` (the username could not be resolved) and `client_missing` (no `httpx`).

## One observation, one derive function

`get_daily_credits` gathers an `Observation` (token present, HTTP client present, how the username lookup went, how each requested day went) and `derive` is the only code that maps it to a state. `BillingStatus` refuses a field combination its state never carries, and `derive` refuses an observation no request sequence can produce (days fetched before the username resolved).

Rules worth knowing:

- A refused or unreachable window is never `ready`. All days refused is `unavailable`; all days failing without a refusal is `unknown`.
- A day failing among successful days makes the window `partial`; the total shown is labelled with the number of missing days.
- Only `unavailable` is held for five minutes so a refused token is not re-asked on every poll. `unknown` is retried on the next poll.
- A day is final once it was fetched after its UTC day ended. An entry fetched while the day was still accruing is refetched once after midnight; today is refreshed at most every five minutes.

## Who reads it

| Reader | How |
|---|---|
| `GET /api/billing` | the derived status; `available` (state is `ready` or `partial`), `reason`, `daily` and `as_of` keep their old meaning, `state`, `reason_kind` and `failed_days` are additive; an unexpected exception is a derived `unknown` |
| `GET /api/config` `billing_pat_configured` | `billing.pat_configured()`, which derives from the same function |
| The card in `app.js` | presents the server's fields; it never decides the mode |

A new reader calls `derive` (through `get_daily_credits`) and does not look at the env var, the caches or the empty list itself.

## Tests

`tests/test_billing_state.py` is the table (one row per reachable observation) plus the proofs that an impossible observation and an illegal status are refused. `tests/test_billing.py` runs `get_daily_credits` over stubbed calls with an injected clock: all-403, all-network-error, mixed success and failure, the 404 hold and the midnight rollover.
