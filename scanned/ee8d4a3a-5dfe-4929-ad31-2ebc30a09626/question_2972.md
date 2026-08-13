# Q2972: lending_account_withdraw: cross-mode collateral view mismatch [a-withdraw-after-permissionless-price] [cycle]

## Question
Can an unprivileged attacker use `lending_account_withdraw` with a withdraw after permissionless price-cache pulse on the same bank so `lending_account_withdraw` evaluates account risk under one mode and settles value under another, violating `withdraw must release only value actually owned by the caller while preserving fresh post-withdraw health and bank solvency` and resulting in `Critical: direct theft or creation of bad debt via over-withdrawal`? Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.

## Target
- File/function: `programs/marginfi/src/instructions/marginfi_account/withdraw.rs` / `lending_account_withdraw`
- Entrypoint: `lending_account_withdraw`
- Attacker controls: a withdraw after permissionless price-cache pulse on the same bank
- Exploit idea: Probe transitions involving eMode, isolated assets, or asset tags where one code path reads stale or differently weighted collateral than the mutating path settles. Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.
- Invariant to test: withdraw must release only value actually owned by the caller while preserving fresh post-withdraw health and bank solvency
- Expected Immunefi impact: Critical: direct theft or creation of bad debt via over-withdrawal
- Fast validation: Create a user that changes the relevant mode/context around the call and assert the instruction cannot accept if recomputation under a single consistent mode would fail. Run the target path in a short deposit/withdraw or borrow/repay style loop and assert no monotonic gain appears.
