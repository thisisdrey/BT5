# Q5699: compression bomb on a compressible protocol field — utils.rs

## Question
Can an unprivileged mainnet account, entering through `broadcast_tx_commit` / `send_tx` RPC carrying a SignedTransaction signed by an ordinary FullAccess key, a highly compressible payload sized to expand enormously on decompression, with a duplicate or out-of-range enum discriminant, and additionally with nesting at exactly the maximum accepted depth, reach `create_receipt_id_from_receipt_id` in `core/primitives/src/utils.rs` and make decompression allocate or spend far more than the accounting expects, breaking the invariant that decompressed size is bounded before decompression proceeds, leading to High - Temporary freezing of network transactions (chunk/block production delayed 500%+ over recent average)?

## Target
- File/function: `core/primitives/src/utils.rs` :: `create_receipt_id_from_receipt_id`
- Entrypoint: `broadcast_tx_commit` / `send_tx` RPC carrying a SignedTransaction signed by an ordinary FullAccess key
- Attacker controls: a highly compressible payload sized to expand enormously on decompression; with a duplicate or out-of-range enum discriminant; with nesting at exactly the maximum accepted depth
- Exploit idea: make decompression allocate or spend far more than the accounting expects
- Invariant to test: decompressed size is bounded before decompression proceeds
- Expected Immunefi impact: High - Temporary freezing of network transactions (chunk/block production delayed 500%+ over recent average)
- Fast validation: unit test decompressing a maximal-ratio payload with the limit enforced
