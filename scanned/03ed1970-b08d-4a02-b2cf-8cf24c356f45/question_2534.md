# Q2534: get_not_unique_leader_tpus can be driven into unbounded work (tpu_info.rs)

## Question
Can an unprivileged attacker entering through one JSON-RPC call or websocket subscription from a single client, at most once per CLUSTER_SLOT_TIME_TARGET/2 reach `get_not_unique_leader_tpus` in `send-transaction-service/src/tpu_info.rs` with an index range the attacker can grow without bound, and make `get_not_unique_leader_tpus` iterate over an attacker-sized set far past the per-slot budget, so that the invariant "Iteration count in `get_not_unique_leader_tpus` is bounded by a constant or by a value the transaction pays for." breaks and the result is Liveness / Loss of Availability?

## Target
- File/function: `send-transaction-service/src/tpu_info.rs` -> `get_not_unique_leader_tpus()` (around line 23)
- Entrypoint: one JSON-RPC call or websocket subscription from a single client, at most once per CLUSTER_SLOT_TIME_TARGET/2
- Attacker controls: an index range the attacker can grow without bound
- Exploit idea: Grow the attacker-controlled collection `get_not_unique_leader_tpus` iterates so a single transaction pins the replay thread far past the slot budget.
- Invariant to test: Iteration count in `get_not_unique_leader_tpus` is bounded by a constant or by a value the transaction pays for.
- Expected Immunefi impact: Liveness / Loss of Availability - consensus halts and requires human intervention (1,250-5,000 SOL)
- Fast validation: Benchmark `get_not_unique_leader_tpus` as the attacker-controlled size grows; assert runtime stays under the per-slot budget.

## Bounty scope note
In-scope target per anza-xyz/agave SECURITY.md. Assumes no validator, leader,
staked-node, peer, gossip, operator, or leaked-key capability. Folder scope:
Critical. An unprivileged remote client can send TPU/QUIC traffic or transaction packets that panic, deadlock, or unboundedly grow memory in ingress, sigverify, scheduling, or PoH, halting block production across the cluster.
