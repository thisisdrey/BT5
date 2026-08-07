# Q0549: try_receive_completed lets one client starve others (scheduler_common.rs)

## Question
Can an unprivileged attacker entering through a transaction that lands in a block and is replayed by every node on the cluster reach `try_receive_completed` in `core/src/banking_stage/transaction_scheduler/scheduler_common.rs` with arguments that drive the path into its error branch after side effects were applied, and occupy the shared capacity `try_receive_completed` arbitrates so honest fee-paying traffic is dropped, so that the invariant "No single source can hold more than its share of the shared capacity `try_receive_completed` manages." breaks and the result is DoS?

## Target
- File/function: `core/src/banking_stage/transaction_scheduler/scheduler_common.rs` -> `try_receive_completed()` (around line 236)
- Entrypoint: a transaction that lands in a block and is replayed by every node on the cluster
- Attacker controls: arguments that drive the path into its error branch after side effects were applied
- Exploit idea: Occupy the shared structure `try_receive_completed` arbitrates so legitimate fee-paying traffic is dropped or indefinitely deferred.
- Invariant to test: No single source can hold more than its share of the shared capacity `try_receive_completed` manages.
- Expected Immunefi impact: DoS - remote resource exhaustion via non-RPC protocols (315-1,250 SOL)
- Fast validation: Run adversarial and honest load together; assert honest throughput stays above its fair share.

## Bounty scope note
In-scope target per anza-xyz/agave SECURITY.md. Assumes no validator, leader,
staked-node, peer, gossip, operator, or leaked-key capability. Folder scope:
High. An unprivileged attacker can cheaply force sigverify, dedup, scheduling, or buffering work that vastly exceeds the fees ever collected, degrading the leader below true cost.
