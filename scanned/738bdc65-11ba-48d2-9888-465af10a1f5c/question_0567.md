# Q567: change_liability_shares: cross-mode collateral view mismatch [a-repay-after-a-liquidation] [cache-order]

## Question
Can an unprivileged attacker use `lending_account_repay` with a repay after a liquidation or flashloan session changed the same bank exposure so `change_liability_shares` evaluates account risk under one mode and settles value under another, violating `repay paths must reduce exactly the debt they settle and cannot erase more liability than the repaid value warrants` and resulting in `High: exploitable debt understatement that can lead to later insolvency or unauthorized withdrawal`? Focus specifically on whether one earlier public instruction in the same transaction can change a cache, flag, or active-balance view before settlement.

## Target
- File/function: `programs/marginfi/src/state/bank.rs` / `change_liability_shares`
- Entrypoint: `lending_account_repay`
- Attacker controls: a repay after a liquidation or flashloan session changed the same bank exposure
- Exploit idea: Probe transitions involving eMode, isolated assets, or asset tags where one code path reads stale or differently weighted collateral than the mutating path settles. Focus specifically on whether one earlier public instruction in the same transaction can change a cache, flag, or active-balance view before settlement.
- Invariant to test: repay paths must reduce exactly the debt they settle and cannot erase more liability than the repaid value warrants
- Expected Immunefi impact: High: exploitable debt understatement that can lead to later insolvency or unauthorized withdrawal
- Fast validation: Create a user that changes the relevant mode/context around the call and assert the instruction cannot accept if recomputation under a single consistent mode would fail. Add an adversarial same-transaction precursor that changes dependent cache or balance state before the target call.
