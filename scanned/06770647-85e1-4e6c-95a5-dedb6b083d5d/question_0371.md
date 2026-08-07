# Q0371: try_add_connection lets one client starve others (simple_qos.rs)

## Question
Can an unprivileged attacker entering through raw QUIC/UDP packets and transaction batches sent by an unstaked client to the leader's public TPU port reach `try_add_connection` in `streamer/src/nonblocking/simple_qos.rs` with an ordering of instructions that leaves partial state from an earlier failure, and occupy the shared capacity `try_add_connection` arbitrates so honest fee-paying traffic is dropped, so that the invariant "No single source can hold more than its share of the shared capacity `try_add_connection` manages." breaks and the result is DoS?

## Target
- File/function: `streamer/src/nonblocking/simple_qos.rs` -> `try_add_connection()` (around line 288)
- Entrypoint: raw QUIC/UDP packets and transaction batches sent by an unstaked client to the leader's public TPU port
- Attacker controls: an ordering of instructions that leaves partial state from an earlier failure
- Exploit idea: Occupy the shared structure `try_add_connection` arbitrates so legitimate fee-paying traffic is dropped or indefinitely deferred.
- Invariant to test: No single source can hold more than its share of the shared capacity `try_add_connection` manages.
- Expected Immunefi impact: DoS - remote resource exhaustion via non-RPC protocols (315-1,250 SOL)
- Fast validation: Run adversarial and honest load together; assert honest throughput stays above its fair share.

## Bounty scope note
In-scope target per anza-xyz/agave SECURITY.md. Assumes no validator, leader,
staked-node, peer, gossip, operator, or leaked-key capability. Folder scope:
High. An unprivileged attacker can cheaply force sigverify, dedup, scheduling, or buffering work that vastly exceeds the fees ever collected, degrading the leader below true cost.
