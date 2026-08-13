# Q2150: calc_interest_rate: public state-unpause or recovery path is insufficiently gated [a-public-accrual-followed-immediately] [binding]

## Question
Can an unprivileged attacker drive `lending_pool_accrue_bank_interest` into `calc_interest_rate` with a public accrual followed immediately by borrow/withdraw/repay actions so a public recovery/unpause path bypasses a required safety condition, violating `public interest accrual must be economically idempotent and cannot mint or erase value beyond the intended deterministic interest update` and causing `High: value leakage or insolvency through public accrual replay`? Focus specifically on exact binding between the helper target and every auxiliary account consumed from remaining accounts.

## Target
- File/function: `programs/marginfi/src/state/interest_rate.rs` / `calc_interest_rate`
- Entrypoint: `lending_pool_accrue_bank_interest`
- Attacker controls: a public accrual followed immediately by borrow/withdraw/repay actions
- Exploit idea: Audit permissionless recovery paths for missing time, authority, or state-boundary checks that could reopen value-moving actions too early. Focus specifically on exact binding between the helper target and every auxiliary account consumed from remaining accounts.
- Invariant to test: public interest accrual must be economically idempotent and cannot mint or erase value beyond the intended deterministic interest update
- Expected Immunefi impact: High: value leakage or insolvency through public accrual replay
- Fast validation: Model the blocked/precondition-failing state and assert the public recovery path rejects until every intended safety condition is met. Supply multiple plausible auxiliary accounts and assert the helper mutates only the fully validated target context.
