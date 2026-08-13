# Q490: change_liability_shares: share minting vs health check desync [a-repay-amount-chosen-to] [cycle]

## Question
Can an unprivileged attacker enter through `lending_account_repay` and make `change_liability_shares` observe a repay amount chosen to maximize floor/ceil asymmetry in liability burn so that share minting/burning and health enforcement are evaluated from inconsistent state, breaking `repay paths must reduce exactly the debt they settle and cannot erase more liability than the repaid value warrants` and leading to `High: exploitable debt understatement that can lead to later insolvency or unauthorized withdrawal`? Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.

## Target
- File/function: `programs/marginfi/src/state/bank.rs` / `change_liability_shares`
- Entrypoint: `lending_account_repay`
- Attacker controls: a repay amount chosen to maximize floor/ceil asymmetry in liability burn
- Exploit idea: Drive pre-state checks and post-state share changes through a boundary case so the instruction accepts a state transition that should fail once all balances are recomputed consistently. Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.
- Invariant to test: repay paths must reduce exactly the debt they settle and cannot erase more liability than the repaid value warrants
- Expected Immunefi impact: High: exploitable debt understatement that can lead to later insolvency or unauthorized withdrawal
- Fast validation: Build an integration test around `lending_account_repay` with the controlled state, then assert that accepted execution leaves post-instruction health negative or value moved beyond the allowed amount. Run the target path in a short deposit/withdraw or borrow/repay style loop and assert no monotonic gain appears.
