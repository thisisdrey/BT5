# Q2980: combined_ema amplifies a cheap input into expensive work (program_metrics.rs)

## Question
Can an unprivileged attacker entering through deploying an attacker-authored sBPF program and invoking it with crafted instruction data and account lists reach `combined_ema` in `program-runtime/src/program_metrics.rs` with an ordering of instructions that leaves partial state from an earlier failure, and make a minimal accepted input to `combined_ema` fan out into disproportionate downstream work, so that the invariant "Work performed is proportional to the size and fee of the input that triggered it." breaks and the result is DoS?

## Target
- File/function: `program-runtime/src/program_metrics.rs` -> `combined_ema()` (around line 224)
- Entrypoint: deploying an attacker-authored sBPF program and invoking it with crafted instruction data and account lists
- Attacker controls: an ordering of instructions that leaves partial state from an earlier failure
- Exploit idea: Send the smallest accepted input that makes `combined_ema` fan out into large downstream work, so a cheap transaction/packet costs the node orders more.
- Invariant to test: Work performed is proportional to the size and fee of the input that triggered it.
- Expected Immunefi impact: DoS - remote resource exhaustion via non-RPC protocols (315-1,250 SOL)
- Fast validation: Plot input bytes versus work done in `combined_ema`; assert the ratio is bounded by a constant.

## Bounty scope note
In-scope target per anza-xyz/agave SECURITY.md. Assumes no validator, leader,
staked-node, peer, gossip, operator, or leaked-key capability. Folder scope:
High. An unprivileged attacker can cheaply force sigverify, dedup, scheduling, or buffering work that vastly exceeds the fees ever collected, degrading the leader below true cost.
