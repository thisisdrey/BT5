# Q3164: free can be driven into unbounded work (pubkeys_ptr.rs)

## Question
Can an unprivileged attacker entering through raw QUIC/UDP packets and transaction batches sent by an unstaked client to the leader's public TPU port reach `free` in `scheduling-utils/src/pubkeys_ptr.rs` with a maximal instruction/account count that pushes the path to its declared limit, and make `free` iterate over an attacker-sized set far past the per-slot budget, so that the invariant "Iteration count in `free` is bounded by a constant or by a value the transaction pays for." breaks and the result is Liveness / Loss of Availability?

## Target
- File/function: `scheduling-utils/src/pubkeys_ptr.rs` -> `free()` (around line 62)
- Entrypoint: raw QUIC/UDP packets and transaction batches sent by an unstaked client to the leader's public TPU port
- Attacker controls: a maximal instruction/account count that pushes the path to its declared limit
- Exploit idea: Grow the attacker-controlled collection `free` iterates so a single transaction pins the replay thread far past the slot budget.
- Invariant to test: Iteration count in `free` is bounded by a constant or by a value the transaction pays for.
- Expected Immunefi impact: Liveness / Loss of Availability - consensus halts and requires human intervention (1,250-5,000 SOL)
- Fast validation: Benchmark `free` as the attacker-controlled size grows; assert runtime stays under the per-slot budget.

## Bounty scope note
In-scope target per anza-xyz/agave SECURITY.md. Assumes no validator, leader,
staked-node, peer, gossip, operator, or leaked-key capability. Folder scope:
Critical. An unprivileged remote client can send TPU/QUIC traffic or transaction packets that panic, deadlock, or unboundedly grow memory in ingress, sigverify, scheduling, or PoH, halting block production across the cluster.
