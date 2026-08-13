# Q2187: calc_interest_rate: permissionless helper mutates blocked objects despite pause or freeze [a-same-slot-sequence-where] [idempotence]

## Question
Can an unprivileged attacker invoke `lending_pool_accrue_bank_interest` with a same-slot sequence where utilization changes before and after accrual so `calc_interest_rate` mutates a paused, frozen, or otherwise blocked object, violating `public interest accrual must be economically idempotent and cannot mint or erase value beyond the intended deterministic interest update` and causing `High: value leakage or insolvency through public accrual replay`? Focus specifically on whether the public helper is truly idempotent under unchanged state.

## Target
- File/function: `programs/marginfi/src/state/interest_rate.rs` / `calc_interest_rate`
- Entrypoint: `lending_pool_accrue_bank_interest`
- Attacker controls: a same-slot sequence where utilization changes before and after accrual
- Exploit idea: Verify that even public helpers respect the same blocking semantics as direct value-moving instructions where required. Focus specifically on whether the public helper is truly idempotent under unchanged state.
- Invariant to test: public interest accrual must be economically idempotent and cannot mint or erase value beyond the intended deterministic interest update
- Expected Immunefi impact: High: value leakage or insolvency through public accrual replay
- Fast validation: Pause/freeze the target state, run the helper, and assert no cache, fee, or balance mutation occurs unless explicitly intended. Invoke the same helper repeatedly against unchanged state and assert protocol value, cache, and destinations do not drift.
