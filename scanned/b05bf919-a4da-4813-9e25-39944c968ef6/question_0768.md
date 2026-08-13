# Q768: check_utilization_ratio: repeatable cycle amplifies tiny accounting drift [an-account-whose-other-positions] [cycle]

## Question
Can an unprivileged attacker repeat `lending_account_borrow` under an account whose other positions make health barely pass before utilization is checked so `check_utilization_ratio` leaks value through a cycle that is individually small but cumulatively breaks `borrow acceptance must never depend on a stale or manipulable utilization view that allows debt beyond bank safety limits` and leads to `High: creation of unsafe bank state or later bad debt`? Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.

## Target
- File/function: `programs/marginfi/src/state/bank.rs` / `check_utilization_ratio`
- Entrypoint: `lending_account_borrow`
- Attacker controls: an account whose other positions make health barely pass before utilization is checked
- Exploit idea: Look for a per-call mismatch that can be looped cheaply without relying on heavy traffic, especially deposit/withdraw, borrow/repay, or accrue/settle cycles. Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.
- Invariant to test: borrow acceptance must never depend on a stale or manipulable utilization view that allows debt beyond bank safety limits
- Expected Immunefi impact: High: creation of unsafe bank state or later bad debt
- Fast validation: Run a deterministic loop of the controlled sequence and assert the attacker cannot monotonically increase assets, reduce liabilities, or move protocol totals. Run the target path in a short deposit/withdraw or borrow/repay style loop and assert no monotonic gain appears.
