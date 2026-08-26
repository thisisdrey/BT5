# Q4273: transaction pool ordering and eviction — sharding.rs

## Question
Can an unprivileged mainnet account, entering through a burst of independently signed transactions submitted across many attacker accounts in one block, a flood of transactions from many attacker keys sized to fill the pool exactly, when the pool is filled exactly to its bound by many attacker keys, and additionally when the same transaction is replayable across a reorg at the window edge, reach `get_receipts_shuffle_salt` in `chain/chain/src/sharding.rs` and evict honest transactions permanently or make pool ordering non-deterministic, breaking the invariant that pool admission and ordering are bounded and deterministic, leading to High - Causing network processing nodes to process transactions from the mempool beyond set parameters?

## Target
- File/function: `chain/chain/src/sharding.rs` :: `get_receipts_shuffle_salt`
- Entrypoint: a burst of independently signed transactions submitted across many attacker accounts in one block
- Attacker controls: a flood of transactions from many attacker keys sized to fill the pool exactly; when the pool is filled exactly to its bound by many attacker keys; when the same transaction is replayable across a reorg at the window edge
- Exploit idea: evict honest transactions permanently or make pool ordering non-deterministic
- Invariant to test: pool admission and ordering are bounded and deterministic
- Expected Immunefi impact: High - Causing network processing nodes to process transactions from the mempool beyond set parameters
- Fast validation: test asserting pool bounds and ordering under a crafted flood
