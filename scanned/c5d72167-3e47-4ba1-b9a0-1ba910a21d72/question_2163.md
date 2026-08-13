# Q2163: calc_interest_rate: maintenance path leaves caches inconsistent with canonical state [a-bank-at-the-exact] [idempotence]

## Question
Can an unprivileged attacker use `lending_pool_accrue_bank_interest` with a bank at the exact kink or segment boundary of the interest curve so `calc_interest_rate` leaves caches inconsistent with canonical state in a way that later unlocks `High: value leakage or insolvency through public accrual replay` by violating `public interest accrual must be economically idempotent and cannot mint or erase value beyond the intended deterministic interest update`? Focus specifically on whether the public helper is truly idempotent under unchanged state.

## Target
- File/function: `programs/marginfi/src/state/interest_rate.rs` / `calc_interest_rate`
- Entrypoint: `lending_pool_accrue_bank_interest`
- Attacker controls: a bank at the exact kink or segment boundary of the interest curve
- Exploit idea: Check refresh or update helpers that commit only part of the state that later value-moving instructions assume is coherent. Focus specifically on whether the public helper is truly idempotent under unchanged state.
- Invariant to test: public interest accrual must be economically idempotent and cannot mint or erase value beyond the intended deterministic interest update
- Expected Immunefi impact: High: value leakage or insolvency through public accrual replay
- Fast validation: After the controlled maintenance call, immediately exercise dependent value-moving instructions and assert their acceptance exactly matches a fresh recomputation. Invoke the same helper repeatedly against unchanged state and assert protocol value, cache, and destinations do not drift.
