# Q0942: pending transaction queue growth under congestion — view_client_actor.rs

## Question
Can an unprivileged mainnet account, entering through a burst of independently signed transactions submitted across many attacker accounts in one block, transactions for a congested shard submitted continuously from many accounts, when transaction conversion cost alone approaches the chunk gas limit, reach `resolve_scan_shards` in `chain/client/src/view_client_actor.rs` and keep the pending queue growing past its parameters so nodes process beyond set limits, breaking the invariant that the pending queue is bounded independently of attacker submission rate, leading to High - Causing network processing nodes to process transactions from the mempool beyond set parameters?

## Target
- File/function: `chain/client/src/view_client_actor.rs` :: `resolve_scan_shards`
- Entrypoint: a burst of independently signed transactions submitted across many attacker accounts in one block
- Attacker controls: transactions for a congested shard submitted continuously from many accounts; when transaction conversion cost alone approaches the chunk gas limit
- Exploit idea: keep the pending queue growing past its parameters so nodes process beyond set limits
- Invariant to test: the pending queue is bounded independently of attacker submission rate
- Expected Immunefi impact: High - Causing network processing nodes to process transactions from the mempool beyond set parameters
- Fast validation: test-loop test measuring queue size under sustained submission
