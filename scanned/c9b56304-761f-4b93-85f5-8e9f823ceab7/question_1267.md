# Q1267: new_from_dir lets one client starve others (snapshot_utils.rs)

## Question
Can an unprivileged attacker entering through a transaction that lands in a block and is replayed by every node on the cluster reach `new_from_dir` in `runtime/src/snapshot_utils.rs` with arguments that drive the path into its error branch after side effects were applied, and occupy the shared capacity `new_from_dir` arbitrates so honest fee-paying traffic is dropped, so that the invariant "No single source can hold more than its share of the shared capacity `new_from_dir` manages." breaks and the result is DoS?

## Target
- File/function: `runtime/src/snapshot_utils.rs` -> `new_from_dir()` (around line 126)
- Entrypoint: a transaction that lands in a block and is replayed by every node on the cluster
- Attacker controls: arguments that drive the path into its error branch after side effects were applied
- Exploit idea: Occupy the shared structure `new_from_dir` arbitrates so legitimate fee-paying traffic is dropped or indefinitely deferred.
- Invariant to test: No single source can hold more than its share of the shared capacity `new_from_dir` manages.
- Expected Immunefi impact: DoS - remote resource exhaustion via non-RPC protocols (315-1,250 SOL)
- Fast validation: Run adversarial and honest load together; assert honest throughput stays above its fair share.

## Bounty scope note
In-scope target per anza-xyz/agave SECURITY.md. Assumes no validator, leader,
staked-node, peer, gossip, operator, or leaked-key capability. Folder scope:
High. An unprivileged attacker can cheaply force sigverify, dedup, scheduling, or buffering work that vastly exceeds the fees ever collected, degrading the leader below true cost.
