# Q2247: update_interest_rates: permissionless fee path can pay the wrong destination [dust-sized-liabilities-that-change] [idempotence]

## Question
Can an unprivileged attacker invoke `lending_pool_accrue_bank_interest` with dust-sized liabilities that change rounding direction so `update_interest_rates` pays fees, insurance, or rewards to the wrong destination, breaking `rate-cache updates must stay consistent with canonical bank totals and cannot be replayed or applied to the wrong state snapshot` and causing `Medium: durable financial inconsistency that enables later extraction or freezing`? Focus specifically on whether the public helper is truly idempotent under unchanged state.

## Target
- File/function: `programs/marginfi/src/state/bank_cache.rs` / `update_interest_rates`
- Entrypoint: `lending_pool_accrue_bank_interest`
- Attacker controls: dust-sized liabilities that change rounding direction
- Exploit idea: Attack destination-account binding and one-time fee accounting for public or semi-public collection paths. Focus specifically on whether the public helper is truly idempotent under unchanged state.
- Invariant to test: rate-cache updates must stay consistent with canonical bank totals and cannot be replayed or applied to the wrong state snapshot
- Expected Immunefi impact: Medium: durable financial inconsistency that enables later extraction or freezing
- Fast validation: Swap candidate destinations in the controlled setup and assert no accepted path credits an unvalidated recipient. Invoke the same helper repeatedly against unchanged state and assert protocol value, cache, and destinations do not drift.
