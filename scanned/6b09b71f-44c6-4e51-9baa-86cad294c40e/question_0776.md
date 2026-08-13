# Q776: check_utilization_ratio: cache refresh ordering permits stale acceptance [a-borrow-after-a-permissionless] [cycle]

## Question
Can an unprivileged attacker call `lending_account_borrow` with a borrow after a permissionless fee or interest maintenance call on the same bank so `check_utilization_ratio` accepts a state transition using stale cache values before refresh or recomputation, violating `borrow acceptance must never depend on a stale or manipulable utilization view that allows debt beyond bank safety limits` and causing `High: creation of unsafe bank state or later bad debt`? Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.

## Target
- File/function: `programs/marginfi/src/state/bank.rs` / `check_utilization_ratio`
- Entrypoint: `lending_account_borrow`
- Attacker controls: a borrow after a permissionless fee or interest maintenance call on the same bank
- Exploit idea: Audit whether the instruction depends on cached bank/account state that can lag behind the exact balances or prices settled later in the same path. Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.
- Invariant to test: borrow acceptance must never depend on a stale or manipulable utilization view that allows debt beyond bank safety limits
- Expected Immunefi impact: High: creation of unsafe bank state or later bad debt
- Fast validation: Prepare mismatched cache and live state, invoke the instruction, and assert acceptance never occurs unless recomputed state would still satisfy the rules. Run the target path in a short deposit/withdraw or borrow/repay style loop and assert no monotonic gain appears.
