# Q779: check_utilization_ratio: cache refresh ordering permits stale acceptance [a-user-that-alternates-tiny] [cache-order]

## Question
Can an unprivileged attacker call `lending_account_borrow` with a user that alternates tiny repay and borrow steps before the target call so `check_utilization_ratio` accepts a state transition using stale cache values before refresh or recomputation, violating `borrow acceptance must never depend on a stale or manipulable utilization view that allows debt beyond bank safety limits` and causing `High: creation of unsafe bank state or later bad debt`? Focus specifically on whether one earlier public instruction in the same transaction can change a cache, flag, or active-balance view before settlement.

## Target
- File/function: `programs/marginfi/src/state/bank.rs` / `check_utilization_ratio`
- Entrypoint: `lending_account_borrow`
- Attacker controls: a user that alternates tiny repay and borrow steps before the target call
- Exploit idea: Audit whether the instruction depends on cached bank/account state that can lag behind the exact balances or prices settled later in the same path. Focus specifically on whether one earlier public instruction in the same transaction can change a cache, flag, or active-balance view before settlement.
- Invariant to test: borrow acceptance must never depend on a stale or manipulable utilization view that allows debt beyond bank safety limits
- Expected Immunefi impact: High: creation of unsafe bank state or later bad debt
- Fast validation: Prepare mismatched cache and live state, invoke the instruction, and assert acceptance never occurs unless recomputed state would still satisfy the rules. Add an adversarial same-transaction precursor that changes dependent cache or balance state before the target call.
