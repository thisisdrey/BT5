# Q2235: update_interest_rates: maintenance refresh commits stale or attacker-selected data [a-public-action-that-mutates] [idempotence]

## Question
Can an unprivileged attacker use `lending_pool_accrue_bank_interest` with a public action that mutates bank totals immediately after accrual so `update_interest_rates` refreshes cache/state from stale or attacker-selected inputs, violating `rate-cache updates must stay consistent with canonical bank totals and cannot be replayed or applied to the wrong state snapshot` and causing `Medium: durable financial inconsistency that enables later extraction or freezing`? Focus specifically on whether the public helper is truly idempotent under unchanged state.

## Target
- File/function: `programs/marginfi/src/state/bank_cache.rs` / `update_interest_rates`
- Entrypoint: `lending_pool_accrue_bank_interest`
- Attacker controls: a public action that mutates bank totals immediately after accrual
- Exploit idea: Check whether a public refresh path trusts remaining accounts or cached intermediates too much before committing state. Focus specifically on whether the public helper is truly idempotent under unchanged state.
- Invariant to test: rate-cache updates must stay consistent with canonical bank totals and cannot be replayed or applied to the wrong state snapshot
- Expected Immunefi impact: Medium: durable financial inconsistency that enables later extraction or freezing
- Fast validation: Supply intentionally mismatched refresh inputs and assert cache/state updates are rejected or recomputed from canonical sources only. Invoke the same helper repeatedly against unchanged state and assert protocol value, cache, and destinations do not drift.
