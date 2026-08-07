# Q1513: migrate_legacy_hardlinks lets one client starve others (snapshot_utils.rs)

## Question
Can an unprivileged attacker entering through a transaction that lands in a block and is replayed by every node on the cluster reach `migrate_legacy_hardlinks` in `runtime/src/snapshot_utils.rs` with a declared cost far below the real cost of the work requested, and occupy the shared capacity `migrate_legacy_hardlinks` arbitrates so honest fee-paying traffic is dropped, so that the invariant "No single source can hold more than its share of the shared capacity `migrate_legacy_hardlinks` manages." breaks and the result is DoS?

## Target
- File/function: `runtime/src/snapshot_utils.rs` -> `migrate_legacy_hardlinks()` (around line 1338)
- Entrypoint: a transaction that lands in a block and is replayed by every node on the cluster
- Attacker controls: a declared cost far below the real cost of the work requested
- Exploit idea: Occupy the shared structure `migrate_legacy_hardlinks` arbitrates so legitimate fee-paying traffic is dropped or indefinitely deferred.
- Invariant to test: No single source can hold more than its share of the shared capacity `migrate_legacy_hardlinks` manages.
- Expected Immunefi impact: DoS - remote resource exhaustion via non-RPC protocols (315-1,250 SOL)
- Fast validation: Run adversarial and honest load together; assert honest throughput stays above its fair share.

## Bounty scope note
In-scope target per anza-xyz/agave SECURITY.md. Assumes no validator, leader,
staked-node, peer, gossip, operator, or leaked-key capability. Folder scope:
High. An unprivileged attacker can cheaply force sigverify, dedup, scheduling, or buffering work that vastly exceeds the fees ever collected, degrading the leader below true cost.
