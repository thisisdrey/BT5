# Q738: check_utilization_ratio: frozen or disabled account still reaches value-moving code [a-borrow-at-the-exact] [cycle]

## Question
Can an unprivileged attacker route `lending_account_borrow` through `check_utilization_ratio` with a borrow at the exact utilization limit boundary so a frozen, disabled, or otherwise blocked account still changes value-bearing state, breaking `borrow acceptance must never depend on a stale or manipulable utilization view that allows debt beyond bank safety limits` and causing `High: creation of unsafe bank state or later bad debt`? Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.

## Target
- File/function: `programs/marginfi/src/state/bank.rs` / `check_utilization_ratio`
- Entrypoint: `lending_account_borrow`
- Attacker controls: a borrow at the exact utilization limit boundary
- Exploit idea: Test whether authority/freeze/disabled checks are performed too late, on the wrong object, or on only part of the execution path. Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.
- Invariant to test: borrow acceptance must never depend on a stale or manipulable utilization view that allows debt beyond bank safety limits
- Expected Immunefi impact: High: creation of unsafe bank state or later bad debt
- Fast validation: Set the relevant flags, execute the controlled call, and assert that no vault transfer, share change, or balance activation occurs. Run the target path in a short deposit/withdraw or borrow/repay style loop and assert no monotonic gain appears.
