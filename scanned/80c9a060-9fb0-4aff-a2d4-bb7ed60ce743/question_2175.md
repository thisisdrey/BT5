# Q2175: calc_interest_rate: maintenance path leaves caches inconsistent with canonical state [an-accrual-call-after-bankruptcy] [idempotence]

## Question
Can an unprivileged attacker use `lending_pool_accrue_bank_interest` with an accrual call after bankruptcy or fee collection changed totals so `calc_interest_rate` leaves caches inconsistent with canonical state in a way that later unlocks `High: value leakage or insolvency through public accrual replay` by violating `public interest accrual must be economically idempotent and cannot mint or erase value beyond the intended deterministic interest update`? Focus specifically on whether the public helper is truly idempotent under unchanged state.

## Target
- File/function: `programs/marginfi/src/state/interest_rate.rs` / `calc_interest_rate`
- Entrypoint: `lending_pool_accrue_bank_interest`
- Attacker controls: an accrual call after bankruptcy or fee collection changed totals
- Exploit idea: Check refresh or update helpers that commit only part of the state that later value-moving instructions assume is coherent. Focus specifically on whether the public helper is truly idempotent under unchanged state.
- Invariant to test: public interest accrual must be economically idempotent and cannot mint or erase value beyond the intended deterministic interest update
- Expected Immunefi impact: High: value leakage or insolvency through public accrual replay
- Fast validation: After the controlled maintenance call, immediately exercise dependent value-moving instructions and assert their acceptance exactly matches a fresh recomputation. Invoke the same helper repeatedly against unchanged state and assert protocol value, cache, and destinations do not drift.
