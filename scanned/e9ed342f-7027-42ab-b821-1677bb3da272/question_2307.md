# Q2307: update_interest_rates: permissionless helper mutates blocked objects despite pause or freeze [a-bank-whose-totals-changed] [idempotence]

## Question
Can an unprivileged attacker invoke `lending_pool_accrue_bank_interest` with a bank whose totals changed between cache read and write in the same slot so `update_interest_rates` mutates a paused, frozen, or otherwise blocked object, violating `rate-cache updates must stay consistent with canonical bank totals and cannot be replayed or applied to the wrong state snapshot` and causing `Medium: durable financial inconsistency that enables later extraction or freezing`? Focus specifically on whether the public helper is truly idempotent under unchanged state.

## Target
- File/function: `programs/marginfi/src/state/bank_cache.rs` / `update_interest_rates`
- Entrypoint: `lending_pool_accrue_bank_interest`
- Attacker controls: a bank whose totals changed between cache read and write in the same slot
- Exploit idea: Verify that even public helpers respect the same blocking semantics as direct value-moving instructions where required. Focus specifically on whether the public helper is truly idempotent under unchanged state.
- Invariant to test: rate-cache updates must stay consistent with canonical bank totals and cannot be replayed or applied to the wrong state snapshot
- Expected Immunefi impact: Medium: durable financial inconsistency that enables later extraction or freezing
- Fast validation: Pause/freeze the target state, run the helper, and assert no cache, fee, or balance mutation occurs unless explicitly intended. Invoke the same helper repeatedly against unchanged state and assert protocol value, cache, and destinations do not drift.
