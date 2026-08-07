# Q3312: false_positive_rate lets one client starve others (deduper.rs)

## Question
Can an unprivileged attacker entering through raw QUIC/UDP packets and transaction batches sent by an unstaked client to the leader's public TPU port reach `false_positive_rate` in `perf/src/deduper.rs` with a request that stays one unit under the limit but repeats within a single transaction, and occupy the shared capacity `false_positive_rate` arbitrates so honest fee-paying traffic is dropped, so that the invariant "No single source can hold more than its share of the shared capacity `false_positive_rate` manages." breaks and the result is DoS?

## Target
- File/function: `perf/src/deduper.rs` -> `false_positive_rate()` (around line 57)
- Entrypoint: raw QUIC/UDP packets and transaction batches sent by an unstaked client to the leader's public TPU port
- Attacker controls: a request that stays one unit under the limit but repeats within a single transaction
- Exploit idea: Occupy the shared structure `false_positive_rate` arbitrates so legitimate fee-paying traffic is dropped or indefinitely deferred.
- Invariant to test: No single source can hold more than its share of the shared capacity `false_positive_rate` manages.
- Expected Immunefi impact: DoS - remote resource exhaustion via non-RPC protocols (315-1,250 SOL)
- Fast validation: Run adversarial and honest load together; assert honest throughput stays above its fair share.

## Bounty scope note
In-scope target per anza-xyz/agave SECURITY.md. Assumes no validator, leader,
staked-node, peer, gossip, operator, or leaked-key capability. Folder scope:
High. An unprivileged attacker can cheaply force sigverify, dedup, scheduling, or buffering work that vastly exceeds the fees ever collected, degrading the leader below true cost.
