# Q3371: last_update grows memory without an enforced bound (quic.rs)

## Question
Can an unprivileged attacker entering through raw QUIC/UDP packets and transaction batches sent by an unstaked client to the leader's public TPU port reach `last_update` in `streamer/src/nonblocking/quic.rs` with a repeated operation that the code assumes happens at most once, and grow the buffer `last_update` feeds without any eviction bound taking effect, so that the invariant "Every container this path writes into has an enforced capacity or eviction policy." breaks and the result is DoS?

## Target
- File/function: `streamer/src/nonblocking/quic.rs` -> `last_update()` (around line 892)
- Entrypoint: raw QUIC/UDP packets and transaction batches sent by an unstaked client to the leader's public TPU port
- Attacker controls: a repeated operation that the code assumes happens at most once
- Exploit idea: Repeatedly drive `last_update` so a buffer, map, or cache it feeds grows without eviction, exhausting node memory below the cost the attacker pays.
- Invariant to test: Every container this path writes into has an enforced capacity or eviction policy.
- Expected Immunefi impact: DoS - remote resource exhaustion via non-RPC protocols (315-1,250 SOL)
- Fast validation: Stress the path and assert the container's size plateaus rather than growing linearly with attacker input.

## Bounty scope note
In-scope target per anza-xyz/agave SECURITY.md. Assumes no validator, leader,
staked-node, peer, gossip, operator, or leaked-key capability. Folder scope:
High. An unprivileged attacker can cheaply force sigverify, dedup, scheduling, or buffering work that vastly exceeds the fees ever collected, degrading the leader below true cost.
