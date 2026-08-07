# Q1284: wipe_account_snapshot_dirs arithmetic overflows on reachable values (snapshot_utils.rs)

## Question
Can an unprivileged attacker entering through a transaction that lands in a block and is replayed by every node on the cluster reach `wipe_account_snapshot_dirs` in `runtime/src/snapshot_utils.rs` with a zero-lamport or exactly-rent-exempt-minus-one account, and make the arithmetic in `wipe_account_snapshot_dirs` overflow, wrap, or divide by zero, so that the invariant "All arithmetic on attacker-controlled values is checked or provably bounded." breaks and the result is Liveness / Loss of Availability?

## Target
- File/function: `runtime/src/snapshot_utils.rs` -> `wipe_account_snapshot_dirs()` (around line 1747)
- Entrypoint: a transaction that lands in a block and is replayed by every node on the cluster
- Attacker controls: a zero-lamport or exactly-rent-exempt-minus-one account
- Exploit idea: Supply values that make `wipe_account_snapshot_dirs` overflow, so debug builds abort and release builds wrap into a nonsensical accounting value.
- Invariant to test: All arithmetic on attacker-controlled values is checked or provably bounded.
- Expected Immunefi impact: Liveness / Loss of Availability - consensus halts and requires human intervention (1,250-5,000 SOL)
- Fast validation: Proptest `wipe_account_snapshot_dirs` across full integer ranges; assert checked arithmetic and no wrap in release mode.

## Bounty scope note
In-scope target per anza-xyz/agave SECURITY.md. Assumes no validator, leader,
staked-node, peer, gossip, operator, or leaked-key capability. Folder scope:
Critical. An unprivileged remote client can send TPU/QUIC traffic or transaction packets that panic, deadlock, or unboundedly grow memory in ingress, sigverify, scheduling, or PoH, halting block production across the cluster.
