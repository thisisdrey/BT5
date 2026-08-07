# Q3216: batch_send lets one client starve others (sendmmsg.rs)

## Question
Can an unprivileged attacker entering through raw QUIC/UDP packets and transaction batches sent by an unstaked client to the leader's public TPU port reach `batch_send` in `streamer/src/sendmmsg.rs` with two transactions in one batch that conflict on an account only one of them declares, and occupy the shared capacity `batch_send` arbitrates so honest fee-paying traffic is dropped, so that the invariant "No single source can hold more than its share of the shared capacity `batch_send` manages." breaks and the result is DoS?

## Target
- File/function: `streamer/src/sendmmsg.rs` -> `batch_send()` (around line 39)
- Entrypoint: raw QUIC/UDP packets and transaction batches sent by an unstaked client to the leader's public TPU port
- Attacker controls: two transactions in one batch that conflict on an account only one of them declares
- Exploit idea: Occupy the shared structure `batch_send` arbitrates so legitimate fee-paying traffic is dropped or indefinitely deferred.
- Invariant to test: No single source can hold more than its share of the shared capacity `batch_send` manages.
- Expected Immunefi impact: DoS - remote resource exhaustion via non-RPC protocols (315-1,250 SOL)
- Fast validation: Run adversarial and honest load together; assert honest throughput stays above its fair share.

## Bounty scope note
In-scope target per anza-xyz/agave SECURITY.md. Assumes no validator, leader,
staked-node, peer, gossip, operator, or leaked-key capability. Folder scope:
High. An unprivileged attacker can cheaply force sigverify, dedup, scheduling, or buffering work that vastly exceeds the fees ever collected, degrading the leader below true cost.
