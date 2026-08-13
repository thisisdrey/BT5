# Q520: change_liability_shares: rounding boundary creates extractable dust [a-repay-after-a-liquidation] [cycle]

## Question
Can an unprivileged attacker use `lending_account_repay` with a repay after a liquidation or flashloan session changed the same bank exposure to push `change_liability_shares` across a rounding edge where protocol totals and user shares no longer match, breaking `repay paths must reduce exactly the debt they settle and cannot erase more liability than the repaid value warrants` and eventually causing `High: exploitable debt understatement that can lead to later insolvency or unauthorized withdrawal`? Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.

## Target
- File/function: `programs/marginfi/src/state/bank.rs` / `change_liability_shares`
- Entrypoint: `lending_account_repay`
- Attacker controls: a repay after a liquidation or flashloan session changed the same bank exposure
- Exploit idea: Search for floor/ceil mismatches between user-facing token amounts and internal share accounting near zero, one-share, or threshold-sized transitions. Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.
- Invariant to test: repay paths must reduce exactly the debt they settle and cannot erase more liability than the repaid value warrants
- Expected Immunefi impact: High: exploitable debt understatement that can lead to later insolvency or unauthorized withdrawal
- Fast validation: Fuzz tiny and boundary amounts around the relevant threshold and assert that repeated cycles cannot increase withdrawable assets or decrease repayable debt. Run the target path in a short deposit/withdraw or borrow/repay style loop and assert no monotonic gain appears.
