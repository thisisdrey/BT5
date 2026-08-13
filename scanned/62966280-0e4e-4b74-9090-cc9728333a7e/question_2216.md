# Q2216: update_interest_rates: permissionless maintenance path can redirect value or accounting [dust-sized-liabilities-that-change] [binding]

## Question
Can an unprivileged attacker call `lending_pool_accrue_bank_interest` with dust-sized liabilities that change rounding direction so `update_interest_rates` performs a permissionless maintenance action against the wrong economic object, violating `rate-cache updates must stay consistent with canonical bank totals and cannot be replayed or applied to the wrong state snapshot` and causing `Medium: durable financial inconsistency that enables later extraction or freezing`? Focus specifically on exact binding between the helper target and every auxiliary account consumed from remaining accounts.

## Target
- File/function: `programs/marginfi/src/state/bank_cache.rs` / `update_interest_rates`
- Entrypoint: `lending_pool_accrue_bank_interest`
- Attacker controls: dust-sized liabilities that change rounding direction
- Exploit idea: Audit account binding and economic side effects for public cranks that are supposed to be safe regardless of caller identity. Focus specifically on exact binding between the helper target and every auxiliary account consumed from remaining accounts.
- Invariant to test: rate-cache updates must stay consistent with canonical bank totals and cannot be replayed or applied to the wrong state snapshot
- Expected Immunefi impact: Medium: durable financial inconsistency that enables later extraction or freezing
- Fast validation: Use multiple same-group objects and assert the controlled call cannot mutate or pay out on any object except the one fully validated. Supply multiple plausible auxiliary accounts and assert the helper mutates only the fully validated target context.
