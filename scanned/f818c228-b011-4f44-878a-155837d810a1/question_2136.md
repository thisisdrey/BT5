# Q2136: calc_interest_rate: public refresh or accrual can be replayed to leak value [a-bank-with-dust-sized] [binding]

## Question
Can an unprivileged attacker replay `lending_pool_accrue_bank_interest` with a bank with dust-sized liabilities or assets near zero thresholds so `calc_interest_rate` applies a value-bearing refresh/accrual effect more than once, violating `public interest accrual must be economically idempotent and cannot mint or erase value beyond the intended deterministic interest update` and causing `High: value leakage or insolvency through public accrual replay`? Focus specifically on exact binding between the helper target and every auxiliary account consumed from remaining accounts.

## Target
- File/function: `programs/marginfi/src/state/interest_rate.rs` / `calc_interest_rate`
- Entrypoint: `lending_pool_accrue_bank_interest`
- Attacker controls: a bank with dust-sized liabilities or assets near zero thresholds
- Exploit idea: Look for public maintenance actions whose accounting effect should be idempotent but may not be guarded as such. Focus specifically on exact binding between the helper target and every auxiliary account consumed from remaining accounts.
- Invariant to test: public interest accrual must be economically idempotent and cannot mint or erase value beyond the intended deterministic interest update
- Expected Immunefi impact: High: value leakage or insolvency through public accrual replay
- Fast validation: Call the maintenance path repeatedly in the same state and assert protocol value and user value remain unchanged after the first legitimate application. Supply multiple plausible auxiliary accounts and assert the helper mutates only the fully validated target context.
