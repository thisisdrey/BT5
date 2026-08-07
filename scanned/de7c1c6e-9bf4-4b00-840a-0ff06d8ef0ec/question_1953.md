# Q1953: remaining_until_next_interval lets one client starve others (stats.rs)

## Question
Can an unprivileged attacker entering through ordinary transactions that create, write, resize, close and reopen accounts the attacker pays for reach `remaining_until_next_interval` in `accounts-db/src/accounts_index/stats.rs` with arguments that drive the path into its error branch after side effects were applied, and occupy the shared capacity `remaining_until_next_interval` arbitrates so honest fee-paying traffic is dropped, so that the invariant "No single source can hold more than its share of the shared capacity `remaining_until_next_interval` manages." breaks and the result is DoS?

## Target
- File/function: `accounts-db/src/accounts_index/stats.rs` -> `remaining_until_next_interval()` (around line 158)
- Entrypoint: ordinary transactions that create, write, resize, close and reopen accounts the attacker pays for
- Attacker controls: arguments that drive the path into its error branch after side effects were applied
- Exploit idea: Occupy the shared structure `remaining_until_next_interval` arbitrates so legitimate fee-paying traffic is dropped or indefinitely deferred.
- Invariant to test: No single source can hold more than its share of the shared capacity `remaining_until_next_interval` manages.
- Expected Immunefi impact: DoS - remote resource exhaustion via non-RPC protocols (315-1,250 SOL)
- Fast validation: Run adversarial and honest load together; assert honest throughput stays above its fair share.

## Bounty scope note
In-scope target per anza-xyz/agave SECURITY.md. Assumes no validator, leader,
staked-node, peer, gossip, operator, or leaked-key capability. Folder scope:
High. An unprivileged attacker can cheaply force sigverify, dedup, scheduling, or buffering work that vastly exceeds the fees ever collected, degrading the leader below true cost.
