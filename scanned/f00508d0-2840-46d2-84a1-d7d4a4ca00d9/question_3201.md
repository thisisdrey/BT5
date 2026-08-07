# Q3201: max_streams_per_ms lets one client starve others (stream_throttle.rs)

## Question
Can an unprivileged attacker entering through raw QUIC/UDP packets and transaction batches sent by an unstaked client to the leader's public TPU port reach `max_streams_per_ms` in `streamer/src/nonblocking/stream_throttle.rs` with a value that makes the limit computation itself overflow into a larger allowance, and occupy the shared capacity `max_streams_per_ms` arbitrates so honest fee-paying traffic is dropped, so that the invariant "No single source can hold more than its share of the shared capacity `max_streams_per_ms` manages." breaks and the result is DoS?

## Target
- File/function: `streamer/src/nonblocking/stream_throttle.rs` -> `max_streams_per_ms()` (around line 190)
- Entrypoint: raw QUIC/UDP packets and transaction batches sent by an unstaked client to the leader's public TPU port
- Attacker controls: a value that makes the limit computation itself overflow into a larger allowance
- Exploit idea: Occupy the shared structure `max_streams_per_ms` arbitrates so legitimate fee-paying traffic is dropped or indefinitely deferred.
- Invariant to test: No single source can hold more than its share of the shared capacity `max_streams_per_ms` manages.
- Expected Immunefi impact: DoS - remote resource exhaustion via non-RPC protocols (315-1,250 SOL)
- Fast validation: Run adversarial and honest load together; assert honest throughput stays above its fair share.

## Bounty scope note
In-scope target per anza-xyz/agave SECURITY.md. Assumes no validator, leader,
staked-node, peer, gossip, operator, or leaked-key capability. Folder scope:
High. An unprivileged attacker can cheaply force sigverify, dedup, scheduling, or buffering work that vastly exceeds the fees ever collected, degrading the leader below true cost.
