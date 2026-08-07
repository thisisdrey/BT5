# Q2016: iter_ones amplifies a cheap input into expensive work (rolling_bit_field.rs)

## Question
Can an unprivileged attacker entering through ordinary transactions that create, write, resize, close and reopen accounts the attacker pays for reach `iter_ones` in `accounts-db/src/rolling_bit_field.rs` with an instruction sequence that re-enters the same code path within one transaction, and make a minimal accepted input to `iter_ones` fan out into disproportionate downstream work, so that the invariant "Work performed is proportional to the size and fee of the input that triggered it." breaks and the result is DoS?

## Target
- File/function: `accounts-db/src/rolling_bit_field.rs` -> `iter_ones()` (around line 301)
- Entrypoint: ordinary transactions that create, write, resize, close and reopen accounts the attacker pays for
- Attacker controls: an instruction sequence that re-enters the same code path within one transaction
- Exploit idea: Send the smallest accepted input that makes `iter_ones` fan out into large downstream work, so a cheap transaction/packet costs the node orders more.
- Invariant to test: Work performed is proportional to the size and fee of the input that triggered it.
- Expected Immunefi impact: DoS - remote resource exhaustion via non-RPC protocols (315-1,250 SOL)
- Fast validation: Plot input bytes versus work done in `iter_ones`; assert the ratio is bounded by a constant.

## Bounty scope note
In-scope target per anza-xyz/agave SECURITY.md. Assumes no validator, leader,
staked-node, peer, gossip, operator, or leaked-key capability. Folder scope:
High. An unprivileged attacker can cheaply force sigverify, dedup, scheduling, or buffering work that vastly exceeds the fees ever collected, degrading the leader below true cost.
