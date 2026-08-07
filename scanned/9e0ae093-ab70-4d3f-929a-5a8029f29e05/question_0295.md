# Q0295: building_off_previous_leader_last_block lets one client starve others (poh_recorder.rs)

## Question
Can an unprivileged attacker entering through raw QUIC/UDP packets and transaction batches sent by an unstaked client to the leader's public TPU port reach `building_off_previous_leader_last_block` in `poh/src/poh_recorder.rs` with a conflict pattern that forces repeated reschedule/retry of the same transaction, and occupy the shared capacity `building_off_previous_leader_last_block` arbitrates so honest fee-paying traffic is dropped, so that the invariant "No single source can hold more than its share of the shared capacity `building_off_previous_leader_last_block` manages." breaks and the result is DoS?

## Target
- File/function: `poh/src/poh_recorder.rs` -> `building_off_previous_leader_last_block()` (around line 905)
- Entrypoint: raw QUIC/UDP packets and transaction batches sent by an unstaked client to the leader's public TPU port
- Attacker controls: a conflict pattern that forces repeated reschedule/retry of the same transaction
- Exploit idea: Occupy the shared structure `building_off_previous_leader_last_block` arbitrates so legitimate fee-paying traffic is dropped or indefinitely deferred.
- Invariant to test: No single source can hold more than its share of the shared capacity `building_off_previous_leader_last_block` manages.
- Expected Immunefi impact: DoS - remote resource exhaustion via non-RPC protocols (315-1,250 SOL)
- Fast validation: Run adversarial and honest load together; assert honest throughput stays above its fair share.

## Bounty scope note
In-scope target per anza-xyz/agave SECURITY.md. Assumes no validator, leader,
staked-node, peer, gossip, operator, or leaked-key capability. Folder scope:
High. An unprivileged attacker can cheaply force sigverify, dedup, scheduling, or buffering work that vastly exceeds the fees ever collected, degrading the leader below true cost.
