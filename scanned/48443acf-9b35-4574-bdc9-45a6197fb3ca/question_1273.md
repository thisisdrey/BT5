# Q1273: purge_old_bank_snapshots_at_startup amplifies a cheap input into expensive work (snapshot_utils.rs)

## Question
Can an unprivileged attacker entering through a transaction that lands in a block and is replayed by every node on the cluster reach `purge_old_bank_snapshots_at_startup` in `runtime/src/snapshot_utils.rs` with a sequence that writes, deletes, and rewrites the same key inside one slot, and make a minimal accepted input to `purge_old_bank_snapshots_at_startup` fan out into disproportionate downstream work, so that the invariant "Work performed is proportional to the size and fee of the input that triggered it." breaks and the result is DoS?

## Target
- File/function: `runtime/src/snapshot_utils.rs` -> `purge_old_bank_snapshots_at_startup()` (around line 1778)
- Entrypoint: a transaction that lands in a block and is replayed by every node on the cluster
- Attacker controls: a sequence that writes, deletes, and rewrites the same key inside one slot
- Exploit idea: Send the smallest accepted input that makes `purge_old_bank_snapshots_at_startup` fan out into large downstream work, so a cheap transaction/packet costs the node orders more.
- Invariant to test: Work performed is proportional to the size and fee of the input that triggered it.
- Expected Immunefi impact: DoS - remote resource exhaustion via non-RPC protocols (315-1,250 SOL)
- Fast validation: Plot input bytes versus work done in `purge_old_bank_snapshots_at_startup`; assert the ratio is bounded by a constant.

## Bounty scope note
In-scope target per anza-xyz/agave SECURITY.md. Assumes no validator, leader,
staked-node, peer, gossip, operator, or leaked-key capability. Folder scope:
High. An unprivileged attacker can cheaply force sigverify, dedup, scheduling, or buffering work that vastly exceeds the fees ever collected, degrading the leader below true cost.
