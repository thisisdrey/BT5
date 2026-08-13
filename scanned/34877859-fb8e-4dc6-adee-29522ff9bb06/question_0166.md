# Q166: get_liability_shares: share minting vs health check desync [a-same-slot-repay-then] [cycle]

## Question
Can an unprivileged attacker enter through `lending_account_borrow` and make `get_liability_shares` observe a same-slot repay-then-borrow sequence around the same bank so that share minting/burning and health enforcement are evaluated from inconsistent state, breaking `borrowed liabilities must always correspond to a conservative increase in repayable debt and never exceed fresh borrow capacity` and leading to `Critical: creation of unbacked debt or protocol insolvency`? Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.

## Target
- File/function: `programs/marginfi/src/state/bank.rs` / `get_liability_shares`
- Entrypoint: `lending_account_borrow`
- Attacker controls: a same-slot repay-then-borrow sequence around the same bank
- Exploit idea: Drive pre-state checks and post-state share changes through a boundary case so the instruction accepts a state transition that should fail once all balances are recomputed consistently. Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.
- Invariant to test: borrowed liabilities must always correspond to a conservative increase in repayable debt and never exceed fresh borrow capacity
- Expected Immunefi impact: Critical: creation of unbacked debt or protocol insolvency
- Fast validation: Build an integration test around `lending_account_borrow` with the controlled state, then assert that accepted execution leaves post-instruction health negative or value moved beyond the allowed amount. Run the target path in a short deposit/withdraw or borrow/repay style loop and assert no monotonic gain appears.
