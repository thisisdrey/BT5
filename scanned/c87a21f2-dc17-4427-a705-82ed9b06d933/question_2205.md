# Q2205: calc_interest_rate: collector or crank can finalize on a partially validated object [a-bank-whose-cached-rates] [idempotence]

## Question
Can an unprivileged attacker call `lending_pool_accrue_bank_interest` with a bank whose cached rates lag its live liabilities by one public action so `calc_interest_rate` finalizes a collection/refresh/crank action after only partial validation, breaking `public interest accrual must be economically idempotent and cannot mint or erase value beyond the intended deterministic interest update` and causing `High: value leakage or insolvency through public accrual replay`? Focus specifically on whether the public helper is truly idempotent under unchanged state.

## Target
- File/function: `programs/marginfi/src/state/interest_rate.rs` / `calc_interest_rate`
- Entrypoint: `lending_pool_accrue_bank_interest`
- Attacker controls: a bank whose cached rates lag its live liabilities by one public action
- Exploit idea: Look for multi-step maintenance flows where early checks do not cover every object later mutated or paid. Focus specifically on whether the public helper is truly idempotent under unchanged state.
- Invariant to test: public interest accrual must be economically idempotent and cannot mint or erase value beyond the intended deterministic interest update
- Expected Immunefi impact: High: value leakage or insolvency through public accrual replay
- Fast validation: Construct a mixed-validity object set and assert every mutated account is included in the path validated before mutation. Invoke the same helper repeatedly against unchanged state and assert protocol value, cache, and destinations do not drift.
