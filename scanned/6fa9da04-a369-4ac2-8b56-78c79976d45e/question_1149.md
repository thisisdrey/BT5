# Q1149: sync_indexer_flags: account migration duplicates or strands value [a-sync-call-after-flashloan] [role-reuse]

## Question
Can an unprivileged attacker use `sync_indexer_flags` with a sync call after flashloan flags were set and cleared so `sync_indexer_flags` duplicates, drops, or strands balances during account migration or transfer, violating `synchronized flags must never diverge from enforceable state in a way that unlocks or blocks value-moving paths incorrectly` and causing `Medium: durable financial inconsistency or account freeze with real impact`? Focus specifically on stale authority, migrated-account, or delegated-account reuse after one valid-looking transition.

## Target
- File/function: `programs/marginfi/src/state/marginfi_account.rs` / `sync_indexer_flags`
- Entrypoint: `sync_indexer_flags`
- Attacker controls: a sync call after flashloan flags were set and cleared
- Exploit idea: Probe migration edges where balances, fees, or authorities are copied then cleared, especially if one half can be replayed or partially completed. Focus specifically on stale authority, migrated-account, or delegated-account reuse after one valid-looking transition.
- Invariant to test: synchronized flags must never diverge from enforceable state in a way that unlocks or blocks value-moving paths incorrectly
- Expected Immunefi impact: Medium: durable financial inconsistency or account freeze with real impact
- Fast validation: Simulate partial completion and replay attempts, then assert total exposure across old and new accounts stays conserved. Model one valid transition first, then retry with the attacker as the next caller and assert authority cannot be reused.
