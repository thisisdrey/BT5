# Q0045: dedup can be driven into unbounded work (deduper.rs)

## Question
Can an unprivileged attacker entering through raw QUIC/UDP packets and transaction batches sent by an unstaked client to the leader's public TPU port reach `dedup` in `perf/src/deduper.rs` with a maximal instruction/account count that pushes the path to its declared limit, and make `dedup` iterate over an attacker-sized set far past the per-slot budget, so that the invariant "Iteration count in `dedup` is bounded by a constant or by a value the transaction pays for." breaks and the result is Liveness / Loss of Availability?

## Target
- File/function: `perf/src/deduper.rs` -> `dedup()` (around line 100)
- Entrypoint: raw QUIC/UDP packets and transaction batches sent by an unstaked client to the leader's public TPU port
- Attacker controls: a maximal instruction/account count that pushes the path to its declared limit
- Exploit idea: Grow the attacker-controlled collection `dedup` iterates so a single transaction pins the replay thread far past the slot budget.
- Invariant to test: Iteration count in `dedup` is bounded by a constant or by a value the transaction pays for.
- Expected Immunefi impact: Liveness / Loss of Availability - consensus halts and requires human intervention (1,250-5,000 SOL)
- Fast validation: Benchmark `dedup` as the attacker-controlled size grows; assert runtime stays under the per-slot budget.

## Bounty scope note
In-scope target per anza-xyz/agave SECURITY.md. Assumes no validator, leader,
staked-node, peer, gossip, operator, or leaked-key capability. Folder scope:
Critical. An unprivileged remote client can send TPU/QUIC traffic or transaction packets that panic, deadlock, or unboundedly grow memory in ingress, sigverify, scheduling, or PoH, halting block production across the cluster.
