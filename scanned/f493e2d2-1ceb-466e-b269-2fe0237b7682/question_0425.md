# Q0425: has_frags lets one client starve others (agave-xdp-prog.rs)

## Question
Can an unprivileged attacker entering through raw QUIC/UDP packets and transaction batches sent by an unstaked client to the leader's public TPU port reach `has_frags` in `xdp-ebpf/src/bin/agave-xdp-prog.rs` with arguments that drive the path into its error branch after side effects were applied, and occupy the shared capacity `has_frags` arbitrates so honest fee-paying traffic is dropped, so that the invariant "No single source can hold more than its share of the shared capacity `has_frags` manages." breaks and the result is DoS?

## Target
- File/function: `xdp-ebpf/src/bin/agave-xdp-prog.rs` -> `has_frags()` (around line 38)
- Entrypoint: raw QUIC/UDP packets and transaction batches sent by an unstaked client to the leader's public TPU port
- Attacker controls: arguments that drive the path into its error branch after side effects were applied
- Exploit idea: Occupy the shared structure `has_frags` arbitrates so legitimate fee-paying traffic is dropped or indefinitely deferred.
- Invariant to test: No single source can hold more than its share of the shared capacity `has_frags` manages.
- Expected Immunefi impact: DoS - remote resource exhaustion via non-RPC protocols (315-1,250 SOL)
- Fast validation: Run adversarial and honest load together; assert honest throughput stays above its fair share.

## Bounty scope note
In-scope target per anza-xyz/agave SECURITY.md. Assumes no validator, leader,
staked-node, peer, gossip, operator, or leaked-key capability. Folder scope:
High. An unprivileged attacker can cheaply force sigverify, dedup, scheduling, or buffering work that vastly exceeds the fees ever collected, degrading the leader below true cost.
