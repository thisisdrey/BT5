# Q3177: only_one_contained amplifies a cheap input into expensive work (thread_aware_account_locks.rs)

## Question
Can an unprivileged attacker entering through raw QUIC/UDP packets and transaction batches sent by an unstaked client to the leader's public TPU port reach `only_one_contained` in `scheduling-utils/src/thread_aware_account_locks.rs` with an ordering of instructions that leaves partial state from an earlier failure, and make a minimal accepted input to `only_one_contained` fan out into disproportionate downstream work, so that the invariant "Work performed is proportional to the size and fee of the input that triggered it." breaks and the result is DoS?

## Target
- File/function: `scheduling-utils/src/thread_aware_account_locks.rs` -> `only_one_contained()` (around line 413)
- Entrypoint: raw QUIC/UDP packets and transaction batches sent by an unstaked client to the leader's public TPU port
- Attacker controls: an ordering of instructions that leaves partial state from an earlier failure
- Exploit idea: Send the smallest accepted input that makes `only_one_contained` fan out into large downstream work, so a cheap transaction/packet costs the node orders more.
- Invariant to test: Work performed is proportional to the size and fee of the input that triggered it.
- Expected Immunefi impact: DoS - remote resource exhaustion via non-RPC protocols (315-1,250 SOL)
- Fast validation: Plot input bytes versus work done in `only_one_contained`; assert the ratio is bounded by a constant.

## Bounty scope note
In-scope target per anza-xyz/agave SECURITY.md. Assumes no validator, leader,
staked-node, peer, gossip, operator, or leaked-key capability. Folder scope:
High. An unprivileged attacker can cheaply force sigverify, dedup, scheduling, or buffering work that vastly exceeds the fees ever collected, degrading the leader below true cost.
