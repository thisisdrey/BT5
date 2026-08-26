# Q5496: transaction pool ordering and eviction — event_type.rs

## Question
Can an unprivileged mainnet account, entering through a burst of independently signed transactions submitted across many attacker accounts in one block, a flood of transactions from many attacker keys sized to fill the pool exactly, when the same transaction is replayable across a reorg at the window edge, and additionally when execution depends on data the witness does not fully determine, reach `from_shard_layout` in `chain/chain/src/resharding/event_type.rs` and evict honest transactions permanently or make pool ordering non-deterministic, breaking the invariant that pool admission and ordering are bounded and deterministic, leading to High - Causing network processing nodes to process transactions from the mempool beyond set parameters?

## Target
- File/function: `chain/chain/src/resharding/event_type.rs` :: `from_shard_layout`
- Entrypoint: a burst of independently signed transactions submitted across many attacker accounts in one block
- Attacker controls: a flood of transactions from many attacker keys sized to fill the pool exactly; when the same transaction is replayable across a reorg at the window edge; when execution depends on data the witness does not fully determine
- Exploit idea: evict honest transactions permanently or make pool ordering non-deterministic
- Invariant to test: pool admission and ordering are bounded and deterministic
- Expected Immunefi impact: High - Causing network processing nodes to process transactions from the mempool beyond set parameters
- Fast validation: test asserting pool bounds and ordering under a crafted flood
