# Q717: check_utilization_ratio: state updated before the transfer outcome is final [a-borrow-immediately-after-a] [cache-order]

## Question
Can an unprivileged attacker make `lending_account_borrow` reach `check_utilization_ratio` with a borrow immediately after a vault-affecting permissionless operation such that accounting mutates before the real token/value transfer is conclusively enforced, breaking `borrow acceptance must never depend on a stale or manipulable utilization view that allows debt beyond bank safety limits` and causing `High: creation of unsafe bank state or later bad debt`? Focus specifically on whether one earlier public instruction in the same transaction can change a cache, flag, or active-balance view before settlement.

## Target
- File/function: `programs/marginfi/src/state/bank.rs` / `check_utilization_ratio`
- Entrypoint: `lending_account_borrow`
- Attacker controls: a borrow immediately after a vault-affecting permissionless operation
- Exploit idea: Check whether partial state mutation can survive a later transfer/accounting edge and leave the user with value or debt inconsistent with actual token movement. Focus specifically on whether one earlier public instruction in the same transaction can change a cache, flag, or active-balance view before settlement.
- Invariant to test: borrow acceptance must never depend on a stale or manipulable utilization view that allows debt beyond bank safety limits
- Expected Immunefi impact: High: creation of unsafe bank state or later bad debt
- Fast validation: Inject the controlled token/account conditions and assert that any downstream failure rolls back all shares, caches, and flags atomically. Add an adversarial same-transaction precursor that changes dependent cache or balance state before the target call.
