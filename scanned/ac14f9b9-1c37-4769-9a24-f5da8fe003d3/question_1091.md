# Q1091: sync_indexer_flags: flag desynchronization enables forbidden transitions [an-account-just-before-and] [role-reuse]

## Question
Can an unprivileged attacker use `sync_indexer_flags` with an account just before and after bankruptcy-like disablement conditions so `sync_indexer_flags` leaves flags inconsistent with real account state, violating `synchronized flags must never diverge from enforceable state in a way that unlocks or blocks value-moving paths incorrectly` and enabling `Medium: durable financial inconsistency or account freeze with real impact`? Focus specifically on stale authority, migrated-account, or delegated-account reuse after one valid-looking transition.

## Target
- File/function: `programs/marginfi/src/state/marginfi_account.rs` / `sync_indexer_flags`
- Entrypoint: `sync_indexer_flags`
- Attacker controls: an account just before and after bankruptcy-like disablement conditions
- Exploit idea: Audit transitions around receivership, flashloan state, frozen state, and migrated accounts for paths that set or clear only part of the state machine. Focus specifically on stale authority, migrated-account, or delegated-account reuse after one valid-looking transition.
- Invariant to test: synchronized flags must never diverge from enforceable state in a way that unlocks or blocks value-moving paths incorrectly
- Expected Immunefi impact: Medium: durable financial inconsistency or account freeze with real impact
- Fast validation: Execute the controlled transition and assert flags, counters, and balances remain mutually consistent before and after rollback or success. Model one valid transition first, then retry with the attacker as the next caller and assert authority cannot be reused.
