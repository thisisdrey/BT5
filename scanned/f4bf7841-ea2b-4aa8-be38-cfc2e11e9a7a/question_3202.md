# Q3202: lending_account_repay: share minting vs health check desync [a-repay-amount-at-the] [cycle]

## Question
Can an unprivileged attacker enter through `lending_account_repay` and make `lending_account_repay` observe a repay amount at the last-liability-share boundary so that share minting/burning and health enforcement are evaluated from inconsistent state, breaking `repay must burn exactly the debt it settles and cannot erase liabilities without equivalent economic repayment` and leading to `High: understated debt enabling later unauthorized withdrawal or protocol loss`? Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.

## Target
- File/function: `programs/marginfi/src/instructions/marginfi_account/repay.rs` / `lending_account_repay`
- Entrypoint: `lending_account_repay`
- Attacker controls: a repay amount at the last-liability-share boundary
- Exploit idea: Drive pre-state checks and post-state share changes through a boundary case so the instruction accepts a state transition that should fail once all balances are recomputed consistently. Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.
- Invariant to test: repay must burn exactly the debt it settles and cannot erase liabilities without equivalent economic repayment
- Expected Immunefi impact: High: understated debt enabling later unauthorized withdrawal or protocol loss
- Fast validation: Build an integration test around `lending_account_repay` with the controlled state, then assert that accepted execution leaves post-instruction health negative or value moved beyond the allowed amount. Run the target path in a short deposit/withdraw or borrow/repay style loop and assert no monotonic gain appears.
