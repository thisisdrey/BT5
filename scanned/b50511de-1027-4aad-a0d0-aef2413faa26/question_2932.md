# Q2932: congestion control allowing new transactions to a congested shard — congestion_info.rs

## Question
Can an unprivileged mainnet account, entering through a burst of independently signed transactions submitted across many attacker accounts in one block, transactions whose receiver resolves to a fully congested shard, submitted through many RPC nodes, when receipt sizes sit exactly on the bandwidth-request granularity boundary, and additionally when the shard is driven exactly onto a congestion threshold, reach `is_fully_congested` in `core/primitives/src/congestion_info.rs` and have transactions admitted past the congestion gate so the mempool is processed beyond set parameters, breaking the invariant that transaction admission respects the receiver shard's congestion level, leading to High - Causing network processing nodes to process transactions from the mempool beyond set parameters?

## Target
- File/function: `core/primitives/src/congestion_info.rs` :: `is_fully_congested`
- Entrypoint: a burst of independently signed transactions submitted across many attacker accounts in one block
- Attacker controls: transactions whose receiver resolves to a fully congested shard, submitted through many RPC nodes; when receipt sizes sit exactly on the bandwidth-request granularity boundary; when the shard is driven exactly onto a congestion threshold
- Exploit idea: have transactions admitted past the congestion gate so the mempool is processed beyond set parameters
- Invariant to test: transaction admission respects the receiver shard's congestion level
- Expected Immunefi impact: High - Causing network processing nodes to process transactions from the mempool beyond set parameters
- Fast validation: test-loop test asserting rejection once the shard is congested
