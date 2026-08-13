# Q2264: update_interest_rates: public refresh or accrual can be replayed to leak value [dust-sized-liabilities-that-change] [binding]

## Question
Can an unprivileged attacker replay `lending_pool_accrue_bank_interest` with dust-sized liabilities that change rounding direction so `update_interest_rates` applies a value-bearing refresh/accrual effect more than once, violating `rate-cache updates must stay consistent with canonical bank totals and cannot be replayed or applied to the wrong state snapshot` and causing `Medium: durable financial inconsistency that enables later extraction or freezing`? Focus specifically on exact binding between the helper target and every auxiliary account consumed from remaining accounts.

## Target
- File/function: `programs/marginfi/src/state/bank_cache.rs` / `update_interest_rates`
- Entrypoint: `lending_pool_accrue_bank_interest`
- Attacker controls: dust-sized liabilities that change rounding direction
- Exploit idea: Look for public maintenance actions whose accounting effect should be idempotent but may not be guarded as such. Focus specifically on exact binding between the helper target and every auxiliary account consumed from remaining accounts.
- Invariant to test: rate-cache updates must stay consistent with canonical bank totals and cannot be replayed or applied to the wrong state snapshot
- Expected Immunefi impact: Medium: durable financial inconsistency that enables later extraction or freezing
- Fast validation: Call the maintenance path repeatedly in the same state and assert protocol value and user value remain unchanged after the first legitimate application. Supply multiple plausible auxiliary accounts and assert the helper mutates only the fully validated target context.
