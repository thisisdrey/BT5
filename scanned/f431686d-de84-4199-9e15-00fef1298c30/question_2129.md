# Q2129: unlock_readonly lets one client starve others (account_locks.rs)

## Question
Can an unprivileged attacker entering through ordinary transactions that create, write, resize, close and reopen accounts the attacker pays for reach `unlock_readonly` in `accounts-db/src/account_locks.rs` with an ordering that releases a lock while the batch is still executing, and occupy the shared capacity `unlock_readonly` arbitrates so honest fee-paying traffic is dropped, so that the invariant "No single source can hold more than its share of the shared capacity `unlock_readonly` manages." breaks and the result is DoS?

## Target
- File/function: `accounts-db/src/account_locks.rs` -> `unlock_readonly()` (around line 111)
- Entrypoint: ordinary transactions that create, write, resize, close and reopen accounts the attacker pays for
- Attacker controls: an ordering that releases a lock while the batch is still executing
- Exploit idea: Occupy the shared structure `unlock_readonly` arbitrates so legitimate fee-paying traffic is dropped or indefinitely deferred.
- Invariant to test: No single source can hold more than its share of the shared capacity `unlock_readonly` manages.
- Expected Immunefi impact: DoS - remote resource exhaustion via non-RPC protocols (315-1,250 SOL)
- Fast validation: Run adversarial and honest load together; assert honest throughput stays above its fair share.

## Bounty scope note
In-scope target per anza-xyz/agave SECURITY.md. Assumes no validator, leader,
staked-node, peer, gossip, operator, or leaked-key capability. Folder scope:
High. An unprivileged attacker can cheaply force sigverify, dedup, scheduling, or buffering work that vastly exceeds the fees ever collected, degrading the leader below true cost.
