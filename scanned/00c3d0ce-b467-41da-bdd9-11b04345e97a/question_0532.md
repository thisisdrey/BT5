# Q532: change_liability_shares: remaining-accounts rebinding of the priced asset path [a-same-transaction-borrow-then] [cycle]

## Question
Can an unprivileged attacker supply a same-transaction borrow-then-repay sequence with boundary-sized values to `lending_account_repay` so that `change_liability_shares` binds the wrong priced asset, bank, or vault path during validation, violating `repay paths must reduce exactly the debt they settle and cannot erase more liability than the repaid value warrants` and leading to `High: exploitable debt understatement that can lead to later insolvency or unauthorized withdrawal`? Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.

## Target
- File/function: `programs/marginfi/src/state/bank.rs` / `change_liability_shares`
- Entrypoint: `lending_account_repay`
- Attacker controls: a same-transaction borrow-then-repay sequence with boundary-sized values
- Exploit idea: Attempt to swap, omit, or reorder remaining accounts so a valid-looking path executes against the wrong economic context without failing ownership/group checks. Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.
- Invariant to test: repay paths must reduce exactly the debt they settle and cannot erase more liability than the repaid value warrants
- Expected Immunefi impact: High: exploitable debt understatement that can lead to later insolvency or unauthorized withdrawal
- Fast validation: Construct two active banks and intentionally permute remaining accounts, then assert the instruction cannot succeed against the wrong bank or oracle context. Run the target path in a short deposit/withdraw or borrow/repay style loop and assert no monotonic gain appears.
