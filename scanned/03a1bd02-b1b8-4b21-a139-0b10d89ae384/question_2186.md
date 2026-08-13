# Q2186: calc_interest_rate: permissionless helper mutates blocked objects despite pause or freeze [a-curve-configuration-at-a] [binding]

## Question
Can an unprivileged attacker invoke `lending_pool_accrue_bank_interest` with a curve configuration at a boundary between adjacent curve segments so `calc_interest_rate` mutates a paused, frozen, or otherwise blocked object, violating `public interest accrual must be economically idempotent and cannot mint or erase value beyond the intended deterministic interest update` and causing `High: value leakage or insolvency through public accrual replay`? Focus specifically on exact binding between the helper target and every auxiliary account consumed from remaining accounts.

## Target
- File/function: `programs/marginfi/src/state/interest_rate.rs` / `calc_interest_rate`
- Entrypoint: `lending_pool_accrue_bank_interest`
- Attacker controls: a curve configuration at a boundary between adjacent curve segments
- Exploit idea: Verify that even public helpers respect the same blocking semantics as direct value-moving instructions where required. Focus specifically on exact binding between the helper target and every auxiliary account consumed from remaining accounts.
- Invariant to test: public interest accrual must be economically idempotent and cannot mint or erase value beyond the intended deterministic interest update
- Expected Immunefi impact: High: value leakage or insolvency through public accrual replay
- Fast validation: Pause/freeze the target state, run the helper, and assert no cache, fee, or balance mutation occurs unless explicitly intended. Supply multiple plausible auxiliary accounts and assert the helper mutates only the fully validated target context.
