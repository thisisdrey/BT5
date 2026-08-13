# Q632: change_liability_shares: balance-slot reuse breaks per-bank accounting [a-repay-after-a-liquidation] [cycle]

## Question
Can an unprivileged attacker trigger `lending_account_repay` with a repay after a liquidation or flashloan session changed the same bank exposure so `change_liability_shares` reuses, closes, or reopens a balance slot in a way that violates `repay paths must reduce exactly the debt they settle and cannot erase more liability than the repaid value warrants` and causes `High: exploitable debt understatement that can lead to later insolvency or unauthorized withdrawal`? Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.

## Target
- File/function: `programs/marginfi/src/state/bank.rs` / `change_liability_shares`
- Entrypoint: `lending_account_repay`
- Attacker controls: a repay after a liquidation or flashloan session changed the same bank exposure
- Exploit idea: Look for state-machine edges where active/inactive balance bookkeeping can be confused around zeroing, reopening, or migration. Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.
- Invariant to test: repay paths must reduce exactly the debt they settle and cannot erase more liability than the repaid value warrants
- Expected Immunefi impact: High: exploitable debt understatement that can lead to later insolvency or unauthorized withdrawal
- Fast validation: Exercise close-to-zero, close, reopen, and reuse sequences for the same user and assert no duplicate or orphaned exposure appears. Run the target path in a short deposit/withdraw or borrow/repay style loop and assert no monotonic gain appears.
