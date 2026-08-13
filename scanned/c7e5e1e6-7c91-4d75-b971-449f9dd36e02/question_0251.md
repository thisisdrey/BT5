# Q251: get_liability_shares: cross-mode collateral view mismatch [a-borrow-when-another-balance] [cache-order]

## Question
Can an unprivileged attacker use `lending_account_borrow` with a borrow when another balance on the account is about to become inactive by dust rounding so `get_liability_shares` evaluates account risk under one mode and settles value under another, violating `borrowed liabilities must always correspond to a conservative increase in repayable debt and never exceed fresh borrow capacity` and resulting in `Critical: creation of unbacked debt or protocol insolvency`? Focus specifically on whether one earlier public instruction in the same transaction can change a cache, flag, or active-balance view before settlement.

## Target
- File/function: `programs/marginfi/src/state/bank.rs` / `get_liability_shares`
- Entrypoint: `lending_account_borrow`
- Attacker controls: a borrow when another balance on the account is about to become inactive by dust rounding
- Exploit idea: Probe transitions involving eMode, isolated assets, or asset tags where one code path reads stale or differently weighted collateral than the mutating path settles. Focus specifically on whether one earlier public instruction in the same transaction can change a cache, flag, or active-balance view before settlement.
- Invariant to test: borrowed liabilities must always correspond to a conservative increase in repayable debt and never exceed fresh borrow capacity
- Expected Immunefi impact: Critical: creation of unbacked debt or protocol insolvency
- Fast validation: Create a user that changes the relevant mode/context around the call and assert the instruction cannot accept if recomputation under a single consistent mode would fail. Add an adversarial same-transaction precursor that changes dependent cache or balance state before the target call.
