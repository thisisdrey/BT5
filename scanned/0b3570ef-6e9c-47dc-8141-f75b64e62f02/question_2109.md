# Q2109: calc_interest_rate: maintenance refresh commits stale or attacker-selected data [a-bank-whose-cached-rates] [idempotence]

## Question
Can an unprivileged attacker use `lending_pool_accrue_bank_interest` with a bank whose cached rates lag its live liabilities by one public action so `calc_interest_rate` refreshes cache/state from stale or attacker-selected inputs, violating `public interest accrual must be economically idempotent and cannot mint or erase value beyond the intended deterministic interest update` and causing `High: value leakage or insolvency through public accrual replay`? Focus specifically on whether the public helper is truly idempotent under unchanged state.

## Target
- File/function: `programs/marginfi/src/state/interest_rate.rs` / `calc_interest_rate`
- Entrypoint: `lending_pool_accrue_bank_interest`
- Attacker controls: a bank whose cached rates lag its live liabilities by one public action
- Exploit idea: Check whether a public refresh path trusts remaining accounts or cached intermediates too much before committing state. Focus specifically on whether the public helper is truly idempotent under unchanged state.
- Invariant to test: public interest accrual must be economically idempotent and cannot mint or erase value beyond the intended deterministic interest update
- Expected Immunefi impact: High: value leakage or insolvency through public accrual replay
- Fast validation: Supply intentionally mismatched refresh inputs and assert cache/state updates are rejected or recomputed from canonical sources only. Invoke the same helper repeatedly against unchanged state and assert protocol value, cache, and destinations do not drift.
