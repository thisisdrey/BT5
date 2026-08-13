# Q2145: calc_interest_rate: public state-unpause or recovery path is insufficiently gated [two-accrual-calls-in-the] [idempotence]

## Question
Can an unprivileged attacker drive `lending_pool_accrue_bank_interest` into `calc_interest_rate` with two accrual calls in the same slot with boundary utilization state so a public recovery/unpause path bypasses a required safety condition, violating `public interest accrual must be economically idempotent and cannot mint or erase value beyond the intended deterministic interest update` and causing `High: value leakage or insolvency through public accrual replay`? Focus specifically on whether the public helper is truly idempotent under unchanged state.

## Target
- File/function: `programs/marginfi/src/state/interest_rate.rs` / `calc_interest_rate`
- Entrypoint: `lending_pool_accrue_bank_interest`
- Attacker controls: two accrual calls in the same slot with boundary utilization state
- Exploit idea: Audit permissionless recovery paths for missing time, authority, or state-boundary checks that could reopen value-moving actions too early. Focus specifically on whether the public helper is truly idempotent under unchanged state.
- Invariant to test: public interest accrual must be economically idempotent and cannot mint or erase value beyond the intended deterministic interest update
- Expected Immunefi impact: High: value leakage or insolvency through public accrual replay
- Fast validation: Model the blocked/precondition-failing state and assert the public recovery path rejects until every intended safety condition is met. Invoke the same helper repeatedly against unchanged state and assert protocol value, cache, and destinations do not drift.
