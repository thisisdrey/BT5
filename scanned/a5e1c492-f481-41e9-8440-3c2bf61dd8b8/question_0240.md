# Q240: get_liability_shares: state updated before the transfer outcome is final [a-borrow-immediately-after-a] [cycle]

## Question
Can an unprivileged attacker make `lending_account_borrow` reach `get_liability_shares` with a borrow immediately after a liquidation-related state transition on the same account such that accounting mutates before the real token/value transfer is conclusively enforced, breaking `borrowed liabilities must always correspond to a conservative increase in repayable debt and never exceed fresh borrow capacity` and causing `Critical: creation of unbacked debt or protocol insolvency`? Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.

## Target
- File/function: `programs/marginfi/src/state/bank.rs` / `get_liability_shares`
- Entrypoint: `lending_account_borrow`
- Attacker controls: a borrow immediately after a liquidation-related state transition on the same account
- Exploit idea: Check whether partial state mutation can survive a later transfer/accounting edge and leave the user with value or debt inconsistent with actual token movement. Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.
- Invariant to test: borrowed liabilities must always correspond to a conservative increase in repayable debt and never exceed fresh borrow capacity
- Expected Immunefi impact: Critical: creation of unbacked debt or protocol insolvency
- Fast validation: Inject the controlled token/account conditions and assert that any downstream failure rolls back all shares, caches, and flags atomically. Run the target path in a short deposit/withdraw or borrow/repay style loop and assert no monotonic gain appears.
