# Q554: change_liability_shares: state updated before the transfer outcome is final [a-repay-amount-chosen-to] [cycle]

## Question
Can an unprivileged attacker make `lending_account_repay` reach `change_liability_shares` with a repay amount chosen to maximize floor/ceil asymmetry in liability burn such that accounting mutates before the real token/value transfer is conclusively enforced, breaking `repay paths must reduce exactly the debt they settle and cannot erase more liability than the repaid value warrants` and causing `High: exploitable debt understatement that can lead to later insolvency or unauthorized withdrawal`? Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.

## Target
- File/function: `programs/marginfi/src/state/bank.rs` / `change_liability_shares`
- Entrypoint: `lending_account_repay`
- Attacker controls: a repay amount chosen to maximize floor/ceil asymmetry in liability burn
- Exploit idea: Check whether partial state mutation can survive a later transfer/accounting edge and leave the user with value or debt inconsistent with actual token movement. Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.
- Invariant to test: repay paths must reduce exactly the debt they settle and cannot erase more liability than the repaid value warrants
- Expected Immunefi impact: High: exploitable debt understatement that can lead to later insolvency or unauthorized withdrawal
- Fast validation: Inject the controlled token/account conditions and assert that any downstream failure rolls back all shares, caches, and flags atomically. Run the target path in a short deposit/withdraw or borrow/repay style loop and assert no monotonic gain appears.
