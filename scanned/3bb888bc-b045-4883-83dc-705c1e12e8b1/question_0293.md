# Q0293: send_and_wait_on_pending_message lets one client starve others (poh_controller.rs)

## Question
Can an unprivileged attacker entering through raw QUIC/UDP packets and transaction batches sent by an unstaked client to the leader's public TPU port reach `send_and_wait_on_pending_message` in `poh/src/poh_controller.rs` with an ordering of instructions that leaves partial state from an earlier failure, and occupy the shared capacity `send_and_wait_on_pending_message` arbitrates so honest fee-paying traffic is dropped, so that the invariant "No single source can hold more than its share of the shared capacity `send_and_wait_on_pending_message` manages." breaks and the result is DoS?

## Target
- File/function: `poh/src/poh_controller.rs` -> `send_and_wait_on_pending_message()` (around line 91)
- Entrypoint: raw QUIC/UDP packets and transaction batches sent by an unstaked client to the leader's public TPU port
- Attacker controls: an ordering of instructions that leaves partial state from an earlier failure
- Exploit idea: Occupy the shared structure `send_and_wait_on_pending_message` arbitrates so legitimate fee-paying traffic is dropped or indefinitely deferred.
- Invariant to test: No single source can hold more than its share of the shared capacity `send_and_wait_on_pending_message` manages.
- Expected Immunefi impact: DoS - remote resource exhaustion via non-RPC protocols (315-1,250 SOL)
- Fast validation: Run adversarial and honest load together; assert honest throughput stays above its fair share.

## Bounty scope note
In-scope target per anza-xyz/agave SECURITY.md. Assumes no validator, leader,
staked-node, peer, gossip, operator, or leaked-key capability. Folder scope:
High. An unprivileged attacker can cheaply force sigverify, dedup, scheduling, or buffering work that vastly exceeds the fees ever collected, degrading the leader below true cost.
