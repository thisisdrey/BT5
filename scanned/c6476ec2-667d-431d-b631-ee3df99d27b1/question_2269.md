# Q2269: update_interest_rates: public refresh or accrual can be replayed to leak value [a-stale-cache-plus-fresh] [idempotence]

## Question
Can an unprivileged attacker replay `lending_pool_accrue_bank_interest` with a stale cache plus fresh totals boundary case so `update_interest_rates` applies a value-bearing refresh/accrual effect more than once, violating `rate-cache updates must stay consistent with canonical bank totals and cannot be replayed or applied to the wrong state snapshot` and causing `Medium: durable financial inconsistency that enables later extraction or freezing`? Focus specifically on whether the public helper is truly idempotent under unchanged state.

## Target
- File/function: `programs/marginfi/src/state/bank_cache.rs` / `update_interest_rates`
- Entrypoint: `lending_pool_accrue_bank_interest`
- Attacker controls: a stale cache plus fresh totals boundary case
- Exploit idea: Look for public maintenance actions whose accounting effect should be idempotent but may not be guarded as such. Focus specifically on whether the public helper is truly idempotent under unchanged state.
- Invariant to test: rate-cache updates must stay consistent with canonical bank totals and cannot be replayed or applied to the wrong state snapshot
- Expected Immunefi impact: Medium: durable financial inconsistency that enables later extraction or freezing
- Fast validation: Call the maintenance path repeatedly in the same state and assert protocol value and user value remain unchanged after the first legitimate application. Invoke the same helper repeatedly against unchanged state and assert protocol value, cache, and destinations do not drift.
