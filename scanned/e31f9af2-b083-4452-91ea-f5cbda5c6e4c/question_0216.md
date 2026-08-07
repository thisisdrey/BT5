# Q0216: interface_ipv4 lets one client starve others (lib.rs)

## Question
Can an unprivileged attacker entering through raw QUIC/UDP packets and transaction batches sent by an unstaked client to the leader's public TPU port reach `interface_ipv4` in `xdp/src/lib.rs` with arguments that drive the path into its error branch after side effects were applied, and occupy the shared capacity `interface_ipv4` arbitrates so honest fee-paying traffic is dropped, so that the invariant "No single source can hold more than its share of the shared capacity `interface_ipv4` manages." breaks and the result is DoS?

## Target
- File/function: `xdp/src/lib.rs` -> `interface_ipv4()` (around line 38)
- Entrypoint: raw QUIC/UDP packets and transaction batches sent by an unstaked client to the leader's public TPU port
- Attacker controls: arguments that drive the path into its error branch after side effects were applied
- Exploit idea: Occupy the shared structure `interface_ipv4` arbitrates so legitimate fee-paying traffic is dropped or indefinitely deferred.
- Invariant to test: No single source can hold more than its share of the shared capacity `interface_ipv4` manages.
- Expected Immunefi impact: DoS - remote resource exhaustion via non-RPC protocols (315-1,250 SOL)
- Fast validation: Run adversarial and honest load together; assert honest throughput stays above its fair share.

## Bounty scope note
In-scope target per anza-xyz/agave SECURITY.md. Assumes no validator, leader,
staked-node, peer, gossip, operator, or leaked-key capability. Folder scope:
High. An unprivileged attacker can cheaply force sigverify, dedup, scheduling, or buffering work that vastly exceeds the fees ever collected, degrading the leader below true cost.
