# Q3417: outgoing metadata group boundaries — outgoing_metadata.rs

## Question
Can an unprivileged mainnet account, entering through an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`, a stream of receipts sized to create a maximal number of metadata groups, when receipt sizes sit exactly on the bandwidth-request granularity boundary, and additionally when the shard is driven exactly onto a congestion threshold, reach `get_metadata_for_shard` in `core/store/src/trie/outgoing_metadata.rs` and make metadata group bookkeeping lose or double-count queued bytes, breaking the invariant that outgoing metadata always reflects the exact bytes and count in the buffer, leading to Critical - Permanent freezing of funds (fix requires hardfork)?

## Target
- File/function: `core/store/src/trie/outgoing_metadata.rs` :: `get_metadata_for_shard`
- Entrypoint: an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`
- Attacker controls: a stream of receipts sized to create a maximal number of metadata groups; when receipt sizes sit exactly on the bandwidth-request granularity boundary; when the shard is driven exactly onto a congestion threshold
- Exploit idea: make metadata group bookkeeping lose or double-count queued bytes
- Invariant to test: outgoing metadata always reflects the exact bytes and count in the buffer
- Expected Immunefi impact: Critical - Permanent freezing of funds (fix requires hardfork)
- Fast validation: store test asserting metadata equals a full buffer scan
