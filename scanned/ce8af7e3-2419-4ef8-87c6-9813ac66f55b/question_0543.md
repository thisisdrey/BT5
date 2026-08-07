# Q0543: select_thread lets one client starve others (scheduler_common.rs)

## Question
Can an unprivileged attacker entering through a transaction that lands in a block and is replayed by every node on the cluster reach `select_thread` in `core/src/banking_stage/transaction_scheduler/scheduler_common.rs` with two transactions in one batch that conflict on an account only one of them declares, and occupy the shared capacity `select_thread` arbitrates so honest fee-paying traffic is dropped, so that the invariant "No single source can hold more than its share of the shared capacity `select_thread` manages." breaks and the result is DoS?

## Target
- File/function: `core/src/banking_stage/transaction_scheduler/scheduler_common.rs` -> `select_thread()` (around line 143)
- Entrypoint: a transaction that lands in a block and is replayed by every node on the cluster
- Attacker controls: two transactions in one batch that conflict on an account only one of them declares
- Exploit idea: Occupy the shared structure `select_thread` arbitrates so legitimate fee-paying traffic is dropped or indefinitely deferred.
- Invariant to test: No single source can hold more than its share of the shared capacity `select_thread` manages.
- Expected Immunefi impact: DoS - remote resource exhaustion via non-RPC protocols (315-1,250 SOL)
- Fast validation: Run adversarial and honest load together; assert honest throughput stays above its fair share.

## Bounty scope note
In-scope target per anza-xyz/agave SECURITY.md. Assumes no validator, leader,
staked-node, peer, gossip, operator, or leaked-key capability. Folder scope:
High. An unprivileged attacker can cheaply force sigverify, dedup, scheduling, or buffering work that vastly exceeds the fees ever collected, degrading the leader below true cost.
