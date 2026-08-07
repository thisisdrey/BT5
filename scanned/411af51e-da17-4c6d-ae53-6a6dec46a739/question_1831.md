# Q1831: batch_insert_tombstone_offsets grows memory without an enforced bound (account_storage_entry.rs)

## Question
Can an unprivileged attacker entering through ordinary transactions that create, write, resize, close and reopen accounts the attacker pays for reach `batch_insert_tombstone_offsets` in `accounts-db/src/account_storage_entry.rs` with a sequence that writes, deletes, and rewrites the same key inside one slot, and grow the buffer `batch_insert_tombstone_offsets` feeds without any eviction bound taking effect, so that the invariant "Every container this path writes into has an enforced capacity or eviction policy." breaks and the result is DoS?

## Target
- File/function: `accounts-db/src/account_storage_entry.rs` -> `batch_insert_tombstone_offsets()` (around line 196)
- Entrypoint: ordinary transactions that create, write, resize, close and reopen accounts the attacker pays for
- Attacker controls: a sequence that writes, deletes, and rewrites the same key inside one slot
- Exploit idea: Repeatedly drive `batch_insert_tombstone_offsets` so a buffer, map, or cache it feeds grows without eviction, exhausting node memory below the cost the attacker pays.
- Invariant to test: Every container this path writes into has an enforced capacity or eviction policy.
- Expected Immunefi impact: DoS - remote resource exhaustion via non-RPC protocols (315-1,250 SOL)
- Fast validation: Stress the path and assert the container's size plateaus rather than growing linearly with attacker input.

## Bounty scope note
In-scope target per anza-xyz/agave SECURITY.md. Assumes no validator, leader,
staked-node, peer, gossip, operator, or leaked-key capability. Folder scope:
High. An unprivileged attacker can cheaply force sigverify, dedup, scheduling, or buffering work that vastly exceeds the fees ever collected, degrading the leader below true cost.
