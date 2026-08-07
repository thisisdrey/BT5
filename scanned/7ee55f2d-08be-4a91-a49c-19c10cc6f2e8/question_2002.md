# Q2002: mark_accounts_obsolete arithmetic overflows on reachable values (obsolete_accounts.rs)

## Question
Can an unprivileged attacker entering through ordinary transactions that create, write, resize, close and reopen accounts the attacker pays for reach `mark_accounts_obsolete` in `accounts-db/src/obsolete_accounts.rs` with an account owned by a program the caller controls, with attacker-chosen data, and make the arithmetic in `mark_accounts_obsolete` overflow, wrap, or divide by zero, so that the invariant "All arithmetic on attacker-controlled values is checked or provably bounded." breaks and the result is Liveness / Loss of Availability?

## Target
- File/function: `accounts-db/src/obsolete_accounts.rs` -> `mark_accounts_obsolete()` (around line 20)
- Entrypoint: ordinary transactions that create, write, resize, close and reopen accounts the attacker pays for
- Attacker controls: an account owned by a program the caller controls, with attacker-chosen data
- Exploit idea: Supply values that make `mark_accounts_obsolete` overflow, so debug builds abort and release builds wrap into a nonsensical accounting value.
- Invariant to test: All arithmetic on attacker-controlled values is checked or provably bounded.
- Expected Immunefi impact: Liveness / Loss of Availability - consensus halts and requires human intervention (1,250-5,000 SOL)
- Fast validation: Proptest `mark_accounts_obsolete` across full integer ranges; assert checked arithmetic and no wrap in release mode.

## Bounty scope note
In-scope target per anza-xyz/agave SECURITY.md. Assumes no validator, leader,
staked-node, peer, gossip, operator, or leaked-key capability. Folder scope:
Critical. An unprivileged remote client can send TPU/QUIC traffic or transaction packets that panic, deadlock, or unboundedly grow memory in ingress, sigverify, scheduling, or PoH, halting block production across the cluster.
