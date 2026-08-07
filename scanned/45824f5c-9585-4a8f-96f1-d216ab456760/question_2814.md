# Q2814: new_tombstone lets one client starve others (program_cache_entry.rs)

## Question
Can an unprivileged attacker entering through deploying an attacker-authored sBPF program and invoking it with crafted instruction data and account lists reach `new_tombstone` in `program-runtime/src/program_cache_entry.rs` with an ordering of instructions that leaves partial state from an earlier failure, and occupy the shared capacity `new_tombstone` arbitrates so honest fee-paying traffic is dropped, so that the invariant "No single source can hold more than its share of the shared capacity `new_tombstone` manages." breaks and the result is DoS?

## Target
- File/function: `program-runtime/src/program_cache_entry.rs` -> `new_tombstone()` (around line 329)
- Entrypoint: deploying an attacker-authored sBPF program and invoking it with crafted instruction data and account lists
- Attacker controls: an ordering of instructions that leaves partial state from an earlier failure
- Exploit idea: Occupy the shared structure `new_tombstone` arbitrates so legitimate fee-paying traffic is dropped or indefinitely deferred.
- Invariant to test: No single source can hold more than its share of the shared capacity `new_tombstone` manages.
- Expected Immunefi impact: DoS - remote resource exhaustion via non-RPC protocols (315-1,250 SOL)
- Fast validation: Run adversarial and honest load together; assert honest throughput stays above its fair share.

## Bounty scope note
In-scope target per anza-xyz/agave SECURITY.md. Assumes no validator, leader,
staked-node, peer, gossip, operator, or leaked-key capability. Folder scope:
High. An unprivileged attacker can cheaply force sigverify, dedup, scheduling, or buffering work that vastly exceeds the fees ever collected, degrading the leader below true cost.
