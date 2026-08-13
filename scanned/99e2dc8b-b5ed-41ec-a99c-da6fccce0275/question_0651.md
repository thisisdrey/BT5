# Q651: check_utilization_ratio: share minting vs health check desync [a-user-that-alternates-tiny] [cache-order]

## Question
Can an unprivileged attacker enter through `lending_account_borrow` and make `check_utilization_ratio` observe a user that alternates tiny repay and borrow steps before the target call so that share minting/burning and health enforcement are evaluated from inconsistent state, breaking `borrow acceptance must never depend on a stale or manipulable utilization view that allows debt beyond bank safety limits` and leading to `High: creation of unsafe bank state or later bad debt`? Focus specifically on whether one earlier public instruction in the same transaction can change a cache, flag, or active-balance view before settlement.

## Target
- File/function: `programs/marginfi/src/state/bank.rs` / `check_utilization_ratio`
- Entrypoint: `lending_account_borrow`
- Attacker controls: a user that alternates tiny repay and borrow steps before the target call
- Exploit idea: Drive pre-state checks and post-state share changes through a boundary case so the instruction accepts a state transition that should fail once all balances are recomputed consistently. Focus specifically on whether one earlier public instruction in the same transaction can change a cache, flag, or active-balance view before settlement.
- Invariant to test: borrow acceptance must never depend on a stale or manipulable utilization view that allows debt beyond bank safety limits
- Expected Immunefi impact: High: creation of unsafe bank state or later bad debt
- Fast validation: Build an integration test around `lending_account_borrow` with the controlled state, then assert that accepted execution leaves post-instruction health negative or value moved beyond the allowed amount. Add an adversarial same-transaction precursor that changes dependent cache or balance state before the target call.
