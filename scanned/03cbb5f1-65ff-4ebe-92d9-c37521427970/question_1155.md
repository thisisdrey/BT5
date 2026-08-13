# Q1155: sync_indexer_flags: indexer-facing flags diverge from enforceable state [an-account-just-before-and] [role-reuse]

## Question
Can an unprivileged attacker make `sync_indexer_flags` drive `sync_indexer_flags` with an account just before and after bankruptcy-like disablement conditions so indexer or auxiliary flags diverge from enforceable state and later unlock `Medium: durable financial inconsistency or account freeze with real impact` by violating `synchronized flags must never diverge from enforceable state in a way that unlocks or blocks value-moving paths incorrectly`? Focus specifically on stale authority, migrated-account, or delegated-account reuse after one valid-looking transition.

## Target
- File/function: `programs/marginfi/src/state/marginfi_account.rs` / `sync_indexer_flags`
- Entrypoint: `sync_indexer_flags`
- Attacker controls: an account just before and after bankruptcy-like disablement conditions
- Exploit idea: Look for user-triggered sync or bookkeeping paths that appear informational but influence later authorization or closeability decisions. Focus specifically on stale authority, migrated-account, or delegated-account reuse after one valid-looking transition.
- Invariant to test: synchronized flags must never diverge from enforceable state in a way that unlocks or blocks value-moving paths incorrectly
- Expected Immunefi impact: Medium: durable financial inconsistency or account freeze with real impact
- Fast validation: Mutate the controlled flags, then immediately call dependent instructions and assert they still enforce the true on-chain state. Model one valid transition first, then retry with the attacker as the next caller and assert authority cannot be reused.
