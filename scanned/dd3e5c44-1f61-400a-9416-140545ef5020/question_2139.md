# Q2139: calc_interest_rate: public refresh or accrual can be replayed to leak value [a-same-slot-sequence-where] [idempotence]

## Question
Can an unprivileged attacker replay `lending_pool_accrue_bank_interest` with a same-slot sequence where utilization changes before and after accrual so `calc_interest_rate` applies a value-bearing refresh/accrual effect more than once, violating `public interest accrual must be economically idempotent and cannot mint or erase value beyond the intended deterministic interest update` and causing `High: value leakage or insolvency through public accrual replay`? Focus specifically on whether the public helper is truly idempotent under unchanged state.

## Target
- File/function: `programs/marginfi/src/state/interest_rate.rs` / `calc_interest_rate`
- Entrypoint: `lending_pool_accrue_bank_interest`
- Attacker controls: a same-slot sequence where utilization changes before and after accrual
- Exploit idea: Look for public maintenance actions whose accounting effect should be idempotent but may not be guarded as such. Focus specifically on whether the public helper is truly idempotent under unchanged state.
- Invariant to test: public interest accrual must be economically idempotent and cannot mint or erase value beyond the intended deterministic interest update
- Expected Immunefi impact: High: value leakage or insolvency through public accrual replay
- Fast validation: Call the maintenance path repeatedly in the same state and assert protocol value and user value remain unchanged after the first legitimate application. Invoke the same helper repeatedly against unchanged state and assert protocol value, cache, and destinations do not drift.
