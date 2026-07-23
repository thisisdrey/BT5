# Q9627: Expiry Drift in new_txn

## Question
Can an unprivileged attacker route crafted expirations or stale duplicates through `mempool/src/logging.rs::new_txn` and keep transactions live or replayable longer than mainnet execution permits?

## Target
- File/function: mempool/src/logging.rs::new_txn
- Entrypoint: Submit transactions through the normal public submission path until mempool admission or batching reaches `new_txn`.
- Attacker controls: transaction batches, sequence numbers, gas prices, expirations, duplicate submissions, and replacement candidates
- Exploit idea: Exploit stale-entry retention or expiry handling mismatches between admission, storage, and eventual execution.
- Invariant to test: A transaction that is stale, expired, or already consumed under execution rules must not remain actionable in mempool logic.
- Expected Immunefi impact: High. Unauthorized transaction execution, replay, signer confusion, signature or proof misbinding, or sequence and authorization bypass that lets an unprivileged attacker cause state transitions on behalf of another user or module context.
- Fast validation: Add a test that advances time or versions across expiry boundaries and asserts stale entries become impossible to admit or re-broadcast.
