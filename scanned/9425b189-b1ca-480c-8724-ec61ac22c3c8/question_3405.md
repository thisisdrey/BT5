# Q3405: prune_unstaked_connections_and_add_new_connection can deadlock or livelock the node (swqos.rs)

## Question
Can an unprivileged attacker entering through raw QUIC/UDP packets and transaction batches sent by an unstaked client to the leader's public TPU port reach `prune_unstaked_connections_and_add_new_connection` in `streamer/src/nonblocking/swqos.rs` with an ordering of instructions that leaves partial state from an earlier failure, and hold two of the locks `prune_unstaked_connections_and_add_new_connection` touches in an order that stalls forward progress, so that the invariant "Locks in `prune_unstaked_connections_and_add_new_connection` are always acquired in a total order and released on every path, including error paths." breaks and the result is Liveness / Loss of Availability?

## Target
- File/function: `streamer/src/nonblocking/swqos.rs` -> `prune_unstaked_connections_and_add_new_connection()` (around line 258)
- Entrypoint: raw QUIC/UDP packets and transaction batches sent by an unstaked client to the leader's public TPU port
- Attacker controls: an ordering of instructions that leaves partial state from an earlier failure
- Exploit idea: Create a lock/channel ordering through `prune_unstaked_connections_and_add_new_connection` that two attacker transactions can hold simultaneously, stalling banking or replay indefinitely.
- Invariant to test: Locks in `prune_unstaked_connections_and_add_new_connection` are always acquired in a total order and released on every path, including error paths.
- Expected Immunefi impact: Liveness / Loss of Availability - consensus halts and requires human intervention (1,250-5,000 SOL)
- Fast validation: Loom or stress test the concurrent path; assert forward progress under adversarial interleavings.

## Bounty scope note
In-scope target per anza-xyz/agave SECURITY.md. Assumes no validator, leader,
staked-node, peer, gossip, operator, or leaked-key capability. Folder scope:
Critical. An unprivileged remote client can send TPU/QUIC traffic or transaction packets that panic, deadlock, or unboundedly grow memory in ingress, sigverify, scheduling, or PoH, halting block production across the cluster.
