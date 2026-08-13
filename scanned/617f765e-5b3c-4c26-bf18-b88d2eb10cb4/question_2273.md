# Q2273: update_interest_rates: public state-unpause or recovery path is insufficiently gated [back-to-back-accrual-attempts] [idempotence]

## Question
Can an unprivileged attacker drive `lending_pool_accrue_bank_interest` into `update_interest_rates` with back-to-back accrual attempts against unchanged bank state so a public recovery/unpause path bypasses a required safety condition, violating `rate-cache updates must stay consistent with canonical bank totals and cannot be replayed or applied to the wrong state snapshot` and causing `Medium: durable financial inconsistency that enables later extraction or freezing`? Focus specifically on whether the public helper is truly idempotent under unchanged state.

## Target
- File/function: `programs/marginfi/src/state/bank_cache.rs` / `update_interest_rates`
- Entrypoint: `lending_pool_accrue_bank_interest`
- Attacker controls: back-to-back accrual attempts against unchanged bank state
- Exploit idea: Audit permissionless recovery paths for missing time, authority, or state-boundary checks that could reopen value-moving actions too early. Focus specifically on whether the public helper is truly idempotent under unchanged state.
- Invariant to test: rate-cache updates must stay consistent with canonical bank totals and cannot be replayed or applied to the wrong state snapshot
- Expected Immunefi impact: Medium: durable financial inconsistency that enables later extraction or freezing
- Fast validation: Model the blocked/precondition-failing state and assert the public recovery path rejects until every intended safety condition is met. Invoke the same helper repeatedly against unchanged state and assert protocol value, cache, and destinations do not drift.
