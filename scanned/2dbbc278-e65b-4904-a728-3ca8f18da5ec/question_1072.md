# Q1072: pending transaction queue growth under congestion — pending_transaction_queue.rs

## Question
Can an unprivileged mainnet account, entering through a burst of independently signed transactions submitted across many attacker accounts in one block, transactions for a congested shard submitted continuously from many accounts, when transaction conversion cost alone approaches the chunk gas limit, reach `remove_certified_block` in `chain/client/src/pending_transaction_queue.rs` and keep the pending queue growing past its parameters so nodes process beyond set limits, breaking the invariant that the pending queue is bounded independently of attacker submission rate, leading to High - Causing network processing nodes to process transactions from the mempool beyond set parameters?

## Target
- File/function: `chain/client/src/pending_transaction_queue.rs` :: `remove_certified_block`
- Entrypoint: a burst of independently signed transactions submitted across many attacker accounts in one block
- Attacker controls: transactions for a congested shard submitted continuously from many accounts; when transaction conversion cost alone approaches the chunk gas limit
- Exploit idea: keep the pending queue growing past its parameters so nodes process beyond set limits
- Invariant to test: the pending queue is bounded independently of attacker submission rate
- Expected Immunefi impact: High - Causing network processing nodes to process transactions from the mempool beyond set parameters
- Fast validation: test-loop test measuring queue size under sustained submission
