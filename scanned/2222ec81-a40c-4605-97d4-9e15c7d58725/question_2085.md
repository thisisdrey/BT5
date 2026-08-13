# Q2085: calc_interest_rate: permissionless maintenance path can redirect value or accounting [a-public-accrual-followed-immediately] [idempotence]

## Question
Can an unprivileged attacker call `lending_pool_accrue_bank_interest` with a public accrual followed immediately by borrow/withdraw/repay actions so `calc_interest_rate` performs a permissionless maintenance action against the wrong economic object, violating `public interest accrual must be economically idempotent and cannot mint or erase value beyond the intended deterministic interest update` and causing `High: value leakage or insolvency through public accrual replay`? Focus specifically on whether the public helper is truly idempotent under unchanged state.

## Target
- File/function: `programs/marginfi/src/state/interest_rate.rs` / `calc_interest_rate`
- Entrypoint: `lending_pool_accrue_bank_interest`
- Attacker controls: a public accrual followed immediately by borrow/withdraw/repay actions
- Exploit idea: Audit account binding and economic side effects for public cranks that are supposed to be safe regardless of caller identity. Focus specifically on whether the public helper is truly idempotent under unchanged state.
- Invariant to test: public interest accrual must be economically idempotent and cannot mint or erase value beyond the intended deterministic interest update
- Expected Immunefi impact: High: value leakage or insolvency through public accrual replay
- Fast validation: Use multiple same-group objects and assert the controlled call cannot mutate or pay out on any object except the one fully validated. Invoke the same helper repeatedly against unchanged state and assert protocol value, cache, and destinations do not drift.
