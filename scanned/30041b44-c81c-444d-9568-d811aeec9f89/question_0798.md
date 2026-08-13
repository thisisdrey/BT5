# Q798: check_utilization_ratio: balance-slot reuse breaks per-bank accounting [a-borrow-immediately-after-a] [cycle]

## Question
Can an unprivileged attacker trigger `lending_account_borrow` with a borrow immediately after a vault-affecting permissionless operation so `check_utilization_ratio` reuses, closes, or reopens a balance slot in a way that violates `borrow acceptance must never depend on a stale or manipulable utilization view that allows debt beyond bank safety limits` and causes `High: creation of unsafe bank state or later bad debt`? Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.

## Target
- File/function: `programs/marginfi/src/state/bank.rs` / `check_utilization_ratio`
- Entrypoint: `lending_account_borrow`
- Attacker controls: a borrow immediately after a vault-affecting permissionless operation
- Exploit idea: Look for state-machine edges where active/inactive balance bookkeeping can be confused around zeroing, reopening, or migration. Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.
- Invariant to test: borrow acceptance must never depend on a stale or manipulable utilization view that allows debt beyond bank safety limits
- Expected Immunefi impact: High: creation of unsafe bank state or later bad debt
- Fast validation: Exercise close-to-zero, close, reopen, and reuse sequences for the same user and assert no duplicate or orphaned exposure appears. Run the target path in a short deposit/withdraw or borrow/repay style loop and assert no monotonic gain appears.
