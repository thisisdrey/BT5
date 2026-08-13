# Q199: get_liability_shares: rounding boundary creates extractable dust [a-user-that-toggles-into] [cache-order]

## Question
Can an unprivileged attacker use `lending_account_borrow` with a user that toggles into or out of eMode just before borrowing to push `get_liability_shares` across a rounding edge where protocol totals and user shares no longer match, breaking `borrowed liabilities must always correspond to a conservative increase in repayable debt and never exceed fresh borrow capacity` and eventually causing `Critical: creation of unbacked debt or protocol insolvency`? Focus specifically on whether one earlier public instruction in the same transaction can change a cache, flag, or active-balance view before settlement.

## Target
- File/function: `programs/marginfi/src/state/bank.rs` / `get_liability_shares`
- Entrypoint: `lending_account_borrow`
- Attacker controls: a user that toggles into or out of eMode just before borrowing
- Exploit idea: Search for floor/ceil mismatches between user-facing token amounts and internal share accounting near zero, one-share, or threshold-sized transitions. Focus specifically on whether one earlier public instruction in the same transaction can change a cache, flag, or active-balance view before settlement.
- Invariant to test: borrowed liabilities must always correspond to a conservative increase in repayable debt and never exceed fresh borrow capacity
- Expected Immunefi impact: Critical: creation of unbacked debt or protocol insolvency
- Fast validation: Fuzz tiny and boundary amounts around the relevant threshold and assert that repeated cycles cannot increase withdrawable assets or decrease repayable debt. Add an adversarial same-transaction precursor that changes dependent cache or balance state before the target call.
