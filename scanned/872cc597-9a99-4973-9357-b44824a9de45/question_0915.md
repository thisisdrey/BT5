# Q0915: special_columns_empty lets one client starve others (blockstore_purge.rs)

## Question
Can an unprivileged attacker entering through a transaction that lands in a block and is replayed by every node on the cluster reach `special_columns_empty` in `ledger/src/blockstore/blockstore_purge.rs` with an ordering of instructions that leaves partial state from an earlier failure, and occupy the shared capacity `special_columns_empty` arbitrates so honest fee-paying traffic is dropped, so that the invariant "No single source can hold more than its share of the shared capacity `special_columns_empty` manages." breaks and the result is DoS?

## Target
- File/function: `ledger/src/blockstore/blockstore_purge.rs` -> `special_columns_empty()` (around line 430)
- Entrypoint: a transaction that lands in a block and is replayed by every node on the cluster
- Attacker controls: an ordering of instructions that leaves partial state from an earlier failure
- Exploit idea: Occupy the shared structure `special_columns_empty` arbitrates so legitimate fee-paying traffic is dropped or indefinitely deferred.
- Invariant to test: No single source can hold more than its share of the shared capacity `special_columns_empty` manages.
- Expected Immunefi impact: DoS - remote resource exhaustion via non-RPC protocols (315-1,250 SOL)
- Fast validation: Run adversarial and honest load together; assert honest throughput stays above its fair share.

## Bounty scope note
In-scope target per anza-xyz/agave SECURITY.md. Assumes no validator, leader,
staked-node, peer, gossip, operator, or leaked-key capability. Folder scope:
High. An unprivileged attacker can cheaply force sigverify, dedup, scheduling, or buffering work that vastly exceeds the fees ever collected, degrading the leader below true cost.
