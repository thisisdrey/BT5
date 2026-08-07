# Q3314: new_random_states amplifies a cheap input into expensive work (deduper.rs)

## Question
Can an unprivileged attacker entering through raw QUIC/UDP packets and transaction batches sent by an unstaked client to the leader's public TPU port reach `new_random_states` in `perf/src/deduper.rs` with a maximal instruction/account count that pushes the path to its declared limit, and make a minimal accepted input to `new_random_states` fan out into disproportionate downstream work, so that the invariant "Work performed is proportional to the size and fee of the input that triggered it." breaks and the result is DoS?

## Target
- File/function: `perf/src/deduper.rs` -> `new_random_states()` (around line 117)
- Entrypoint: raw QUIC/UDP packets and transaction batches sent by an unstaked client to the leader's public TPU port
- Attacker controls: a maximal instruction/account count that pushes the path to its declared limit
- Exploit idea: Send the smallest accepted input that makes `new_random_states` fan out into large downstream work, so a cheap transaction/packet costs the node orders more.
- Invariant to test: Work performed is proportional to the size and fee of the input that triggered it.
- Expected Immunefi impact: DoS - remote resource exhaustion via non-RPC protocols (315-1,250 SOL)
- Fast validation: Plot input bytes versus work done in `new_random_states`; assert the ratio is bounded by a constant.

## Bounty scope note
In-scope target per anza-xyz/agave SECURITY.md. Assumes no validator, leader,
staked-node, peer, gossip, operator, or leaked-key capability. Folder scope:
High. An unprivileged attacker can cheaply force sigverify, dedup, scheduling, or buffering work that vastly exceeds the fees ever collected, degrading the leader below true cost.
