# Q2961: lending_account_withdraw: cross-mode collateral view mismatch [a-withdraw-amount-at-the] [cache-order]

## Question
Can an unprivileged attacker use `lending_account_withdraw` with a withdraw amount at the last-share boundary so `lending_account_withdraw` evaluates account risk under one mode and settles value under another, violating `withdraw must release only value actually owned by the caller while preserving fresh post-withdraw health and bank solvency` and resulting in `Critical: direct theft or creation of bad debt via over-withdrawal`? Focus specifically on whether one earlier public instruction in the same transaction can change a cache, flag, or active-balance view before settlement.

## Target
- File/function: `programs/marginfi/src/instructions/marginfi_account/withdraw.rs` / `lending_account_withdraw`
- Entrypoint: `lending_account_withdraw`
- Attacker controls: a withdraw amount at the last-share boundary
- Exploit idea: Probe transitions involving eMode, isolated assets, or asset tags where one code path reads stale or differently weighted collateral than the mutating path settles. Focus specifically on whether one earlier public instruction in the same transaction can change a cache, flag, or active-balance view before settlement.
- Invariant to test: withdraw must release only value actually owned by the caller while preserving fresh post-withdraw health and bank solvency
- Expected Immunefi impact: Critical: direct theft or creation of bad debt via over-withdrawal
- Fast validation: Create a user that changes the relevant mode/context around the call and assert the instruction cannot accept if recomputation under a single consistent mode would fail. Add an adversarial same-transaction precursor that changes dependent cache or balance state before the target call.
