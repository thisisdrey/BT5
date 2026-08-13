# Q734: check_utilization_ratio: cross-mode collateral view mismatch [a-borrow-immediately-after-a] [cycle]

## Question
Can an unprivileged attacker use `lending_account_borrow` with a borrow immediately after a vault-affecting permissionless operation so `check_utilization_ratio` evaluates account risk under one mode and settles value under another, violating `borrow acceptance must never depend on a stale or manipulable utilization view that allows debt beyond bank safety limits` and resulting in `High: creation of unsafe bank state or later bad debt`? Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.

## Target
- File/function: `programs/marginfi/src/state/bank.rs` / `check_utilization_ratio`
- Entrypoint: `lending_account_borrow`
- Attacker controls: a borrow immediately after a vault-affecting permissionless operation
- Exploit idea: Probe transitions involving eMode, isolated assets, or asset tags where one code path reads stale or differently weighted collateral than the mutating path settles. Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.
- Invariant to test: borrow acceptance must never depend on a stale or manipulable utilization view that allows debt beyond bank safety limits
- Expected Immunefi impact: High: creation of unsafe bank state or later bad debt
- Fast validation: Create a user that changes the relevant mode/context around the call and assert the instruction cannot accept if recomputation under a single consistent mode would fail. Run the target path in a short deposit/withdraw or borrow/repay style loop and assert no monotonic gain appears.
