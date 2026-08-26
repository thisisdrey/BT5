# Q0597: congestion control allowing new transactions to a congested shard — receipt.rs

## Question
Can an unprivileged mainnet account, entering through a burst of independently signed transactions submitted across many attacker accounts in one block, transactions whose receiver resolves to a fully congested shard, submitted through many RPC nodes, when receipt sizes sit exactly on the bandwidth-request granularity boundary, reach `target_shard` in `core/primitives/src/receipt.rs` and have transactions admitted past the congestion gate so the mempool is processed beyond set parameters, breaking the invariant that transaction admission respects the receiver shard's congestion level, leading to High - Causing network processing nodes to process transactions from the mempool beyond set parameters?

## Target
- File/function: `core/primitives/src/receipt.rs` :: `target_shard`
- Entrypoint: a burst of independently signed transactions submitted across many attacker accounts in one block
- Attacker controls: transactions whose receiver resolves to a fully congested shard, submitted through many RPC nodes; when receipt sizes sit exactly on the bandwidth-request granularity boundary
- Exploit idea: have transactions admitted past the congestion gate so the mempool is processed beyond set parameters
- Invariant to test: transaction admission respects the receiver shard's congestion level
- Expected Immunefi impact: High - Causing network processing nodes to process transactions from the mempool beyond set parameters
- Fast validation: test-loop test asserting rejection once the shard is congested
