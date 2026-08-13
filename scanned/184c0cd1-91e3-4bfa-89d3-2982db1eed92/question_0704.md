# Q704: check_utilization_ratio: remaining-accounts rebinding of the priced asset path [an-account-whose-other-positions] [cycle]

## Question
Can an unprivileged attacker supply an account whose other positions make health barely pass before utilization is checked to `lending_account_borrow` so that `check_utilization_ratio` binds the wrong priced asset, bank, or vault path during validation, violating `borrow acceptance must never depend on a stale or manipulable utilization view that allows debt beyond bank safety limits` and leading to `High: creation of unsafe bank state or later bad debt`? Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.

## Target
- File/function: `programs/marginfi/src/state/bank.rs` / `check_utilization_ratio`
- Entrypoint: `lending_account_borrow`
- Attacker controls: an account whose other positions make health barely pass before utilization is checked
- Exploit idea: Attempt to swap, omit, or reorder remaining accounts so a valid-looking path executes against the wrong economic context without failing ownership/group checks. Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.
- Invariant to test: borrow acceptance must never depend on a stale or manipulable utilization view that allows debt beyond bank safety limits
- Expected Immunefi impact: High: creation of unsafe bank state or later bad debt
- Fast validation: Construct two active banks and intentionally permute remaining accounts, then assert the instruction cannot succeed against the wrong bank or oracle context. Run the target path in a short deposit/withdraw or borrow/repay style loop and assert no monotonic gain appears.
