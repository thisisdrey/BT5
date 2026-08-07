# Q1193: withdraw_authority amplifies a cheap input into expensive work (non_circulating_supply.rs)

## Question
Can an unprivileged attacker entering through a transaction that lands in a block and is replayed by every node on the cluster reach `withdraw_authority` in `runtime/src/non_circulating_supply.rs` with an ordering of instructions that leaves partial state from an earlier failure, and make a minimal accepted input to `withdraw_authority` fan out into disproportionate downstream work, so that the invariant "Work performed is proportional to the size and fee of the input that triggered it." breaks and the result is DoS?

## Target
- File/function: `runtime/src/non_circulating_supply.rs` -> `withdraw_authority()` (around line 303)
- Entrypoint: a transaction that lands in a block and is replayed by every node on the cluster
- Attacker controls: an ordering of instructions that leaves partial state from an earlier failure
- Exploit idea: Send the smallest accepted input that makes `withdraw_authority` fan out into large downstream work, so a cheap transaction/packet costs the node orders more.
- Invariant to test: Work performed is proportional to the size and fee of the input that triggered it.
- Expected Immunefi impact: DoS - remote resource exhaustion via non-RPC protocols (315-1,250 SOL)
- Fast validation: Plot input bytes versus work done in `withdraw_authority`; assert the ratio is bounded by a constant.

## Bounty scope note
In-scope target per anza-xyz/agave SECURITY.md. Assumes no validator, leader,
staked-node, peer, gossip, operator, or leaked-key capability. Folder scope:
High. An unprivileged attacker can cheaply force sigverify, dedup, scheduling, or buffering work that vastly exceeds the fees ever collected, degrading the leader below true cost.
