# Q3401: on_stream_error lets one client starve others (swqos.rs)

## Question
Can an unprivileged attacker entering through raw QUIC/UDP packets and transaction batches sent by an unstaked client to the leader's public TPU port reach `on_stream_error` in `streamer/src/nonblocking/swqos.rs` with an instruction sequence that re-enters the same code path within one transaction, and occupy the shared capacity `on_stream_error` arbitrates so honest fee-paying traffic is dropped, so that the invariant "No single source can hold more than its share of the shared capacity `on_stream_error` manages." breaks and the result is DoS?

## Target
- File/function: `streamer/src/nonblocking/swqos.rs` -> `on_stream_error()` (around line 456)
- Entrypoint: raw QUIC/UDP packets and transaction batches sent by an unstaked client to the leader's public TPU port
- Attacker controls: an instruction sequence that re-enters the same code path within one transaction
- Exploit idea: Occupy the shared structure `on_stream_error` arbitrates so legitimate fee-paying traffic is dropped or indefinitely deferred.
- Invariant to test: No single source can hold more than its share of the shared capacity `on_stream_error` manages.
- Expected Immunefi impact: DoS - remote resource exhaustion via non-RPC protocols (315-1,250 SOL)
- Fast validation: Run adversarial and honest load together; assert honest throughput stays above its fair share.

## Bounty scope note
In-scope target per anza-xyz/agave SECURITY.md. Assumes no validator, leader,
staked-node, peer, gossip, operator, or leaked-key capability. Folder scope:
High. An unprivileged attacker can cheaply force sigverify, dedup, scheduling, or buffering work that vastly exceeds the fees ever collected, degrading the leader below true cost.
