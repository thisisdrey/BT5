# Q2321: update_interest_rates: collector or crank can finalize on a partially validated object [back-to-back-accrual-attempts] [idempotence]

## Question
Can an unprivileged attacker call `lending_pool_accrue_bank_interest` with back-to-back accrual attempts against unchanged bank state so `update_interest_rates` finalizes a collection/refresh/crank action after only partial validation, breaking `rate-cache updates must stay consistent with canonical bank totals and cannot be replayed or applied to the wrong state snapshot` and causing `Medium: durable financial inconsistency that enables later extraction or freezing`? Focus specifically on whether the public helper is truly idempotent under unchanged state.

## Target
- File/function: `programs/marginfi/src/state/bank_cache.rs` / `update_interest_rates`
- Entrypoint: `lending_pool_accrue_bank_interest`
- Attacker controls: back-to-back accrual attempts against unchanged bank state
- Exploit idea: Look for multi-step maintenance flows where early checks do not cover every object later mutated or paid. Focus specifically on whether the public helper is truly idempotent under unchanged state.
- Invariant to test: rate-cache updates must stay consistent with canonical bank totals and cannot be replayed or applied to the wrong state snapshot
- Expected Immunefi impact: Medium: durable financial inconsistency that enables later extraction or freezing
- Fast validation: Construct a mixed-validity object set and assert every mutated account is included in the path validated before mutation. Invoke the same helper repeatedly against unchanged state and assert protocol value, cache, and destinations do not drift.
