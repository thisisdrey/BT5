# Q1057: sync_indexer_flags: authority binding bypass on account state mutation [an-account-at-the-boundary] [role-reuse]

## Question
Can an unprivileged attacker call `sync_indexer_flags` and make `sync_indexer_flags` accept an account at the boundary between healthy and unhealthy after tiny value changes so another user's account state mutates without valid authority, violating `synchronized flags must never diverge from enforceable state in a way that unlocks or blocks value-moving paths incorrectly` and leading to `Medium: durable financial inconsistency or account freeze with real impact`? Focus specifically on stale authority, migrated-account, or delegated-account reuse after one valid-looking transition.

## Target
- File/function: `programs/marginfi/src/state/marginfi_account.rs` / `sync_indexer_flags`
- Entrypoint: `sync_indexer_flags`
- Attacker controls: an account at the boundary between healthy and unhealthy after tiny value changes
- Exploit idea: Probe signer checks, transferred authorities, PDA ownership, and migrated-account state so public calls cannot rewrite who controls a margin account. Focus specifically on stale authority, migrated-account, or delegated-account reuse after one valid-looking transition.
- Invariant to test: synchronized flags must never diverge from enforceable state in a way that unlocks or blocks value-moving paths incorrectly
- Expected Immunefi impact: Medium: durable financial inconsistency or account freeze with real impact
- Fast validation: Use two users and assert the controlled call cannot mutate the victim account's owner, flags, balances, or closeability. Model one valid transition first, then retry with the attacker as the next caller and assert authority cannot be reused.
