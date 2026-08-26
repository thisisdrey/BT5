# Q2757: transaction pool ordering and eviction — v3.rs

## Question
Can an unprivileged mainnet account, entering through a burst of independently signed transactions submitted across many attacker accounts in one block, a flood of transactions from many attacker keys sized to fill the pool exactly, when transaction conversion cost alone approaches the chunk gas limit, and additionally when the pool is filled exactly to its bound by many attacker keys, reach `resolve_to_current_shard` in `core/primitives/src/shard_layout/v3.rs` and evict honest transactions permanently or make pool ordering non-deterministic, breaking the invariant that pool admission and ordering are bounded and deterministic, leading to High - Causing network processing nodes to process transactions from the mempool beyond set parameters?

## Target
- File/function: `core/primitives/src/shard_layout/v3.rs` :: `resolve_to_current_shard`
- Entrypoint: a burst of independently signed transactions submitted across many attacker accounts in one block
- Attacker controls: a flood of transactions from many attacker keys sized to fill the pool exactly; when transaction conversion cost alone approaches the chunk gas limit; when the pool is filled exactly to its bound by many attacker keys
- Exploit idea: evict honest transactions permanently or make pool ordering non-deterministic
- Invariant to test: pool admission and ordering are bounded and deterministic
- Expected Immunefi impact: High - Causing network processing nodes to process transactions from the mempool beyond set parameters
- Fast validation: test asserting pool bounds and ordering under a crafted flood
