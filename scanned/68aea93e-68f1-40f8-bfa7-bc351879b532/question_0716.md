# Q716: check_utilization_ratio: state updated before the transfer outcome is final [a-user-that-alternates-tiny] [cycle]

## Question
Can an unprivileged attacker make `lending_account_borrow` reach `check_utilization_ratio` with a user that alternates tiny repay and borrow steps before the target call such that accounting mutates before the real token/value transfer is conclusively enforced, breaking `borrow acceptance must never depend on a stale or manipulable utilization view that allows debt beyond bank safety limits` and causing `High: creation of unsafe bank state or later bad debt`? Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.

## Target
- File/function: `programs/marginfi/src/state/bank.rs` / `check_utilization_ratio`
- Entrypoint: `lending_account_borrow`
- Attacker controls: a user that alternates tiny repay and borrow steps before the target call
- Exploit idea: Check whether partial state mutation can survive a later transfer/accounting edge and leave the user with value or debt inconsistent with actual token movement. Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.
- Invariant to test: borrow acceptance must never depend on a stale or manipulable utilization view that allows debt beyond bank safety limits
- Expected Immunefi impact: High: creation of unsafe bank state or later bad debt
- Fast validation: Inject the controlled token/account conditions and assert that any downstream failure rolls back all shares, caches, and flags atomically. Run the target path in a short deposit/withdraw or borrow/repay style loop and assert no monotonic gain appears.
