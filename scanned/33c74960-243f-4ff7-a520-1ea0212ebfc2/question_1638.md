# Q1638: address lets one client starve others (nonce_info.rs)

## Question
Can an unprivileged attacker entering through a transaction broadcast to a public TPU/QUIC endpoint by an ordinary funded keypair reach `address` in `svm/src/nonce_info.rs` with arguments that drive the path into its error branch after side effects were applied, and occupy the shared capacity `address` arbitrates so honest fee-paying traffic is dropped, so that the invariant "No single source can hold more than its share of the shared capacity `address` manages." breaks and the result is DoS?

## Target
- File/function: `svm/src/nonce_info.rs` -> `address()` (around line 58)
- Entrypoint: a transaction broadcast to a public TPU/QUIC endpoint by an ordinary funded keypair
- Attacker controls: arguments that drive the path into its error branch after side effects were applied
- Exploit idea: Occupy the shared structure `address` arbitrates so legitimate fee-paying traffic is dropped or indefinitely deferred.
- Invariant to test: No single source can hold more than its share of the shared capacity `address` manages.
- Expected Immunefi impact: DoS - remote resource exhaustion via non-RPC protocols (315-1,250 SOL)
- Fast validation: Run adversarial and honest load together; assert honest throughput stays above its fair share.

## Bounty scope note
In-scope target per anza-xyz/agave SECURITY.md. Assumes no validator, leader,
staked-node, peer, gossip, operator, or leaked-key capability. Folder scope:
High. An unprivileged attacker can cheaply force sigverify, dedup, scheduling, or buffering work that vastly exceeds the fees ever collected, degrading the leader below true cost.
