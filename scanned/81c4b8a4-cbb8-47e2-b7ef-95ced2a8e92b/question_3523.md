# Q3523: max_num_workers quota accounting can be evaded (banking_stage.rs)

## Question
Can an unprivileged attacker entering through a transaction that lands in a block and is replayed by every node on the cluster reach `max_num_workers` in `core/src/banking_stage.rs` with a request that stays one unit under the limit but repeats within a single transaction, and make the replay result on a fresh node disagree with the replay result on a node warm from cache, so that the invariant "Every unit of resource consumed is attributed to the source that consumed it, for the whole life of the connection/batch." breaks and the result is DoS?

## Target
- File/function: `core/src/banking_stage.rs` -> `max_num_workers()` (around line 649)
- Entrypoint: a transaction that lands in a block and is replayed by every node on the cluster
- Attacker controls: a request that stays one unit under the limit but repeats within a single transaction
- Exploit idea: Structure requests so the resource is consumed but `max_num_workers`'s accounting attributes it elsewhere, to no one, or resets it.
- Invariant to test: Every unit of resource consumed is attributed to the source that consumed it, for the whole life of the connection/batch.
- Expected Immunefi impact: DoS - remote resource exhaustion via non-RPC protocols (315-1,250 SOL)
- Fast validation: Drive the path from one source and assert the accounted usage matches the measured usage exactly.

## Bounty scope note
In-scope target per anza-xyz/agave SECURITY.md. Assumes no validator, leader,
staked-node, peer, gossip, operator, or leaked-key capability. Folder scope:
High. An unprivileged attacker can cheaply force sigverify, dedup, scheduling, or buffering work that vastly exceeds the fees ever collected, degrading the leader below true cost.
