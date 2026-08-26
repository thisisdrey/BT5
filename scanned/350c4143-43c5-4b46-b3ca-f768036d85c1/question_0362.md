# Q0362: recursive structure depth on deserialisation — types.rs

## Question
Can an unprivileged mainnet account, entering through `broadcast_tx_commit` / `send_tx` RPC carrying a SignedTransaction signed by an ordinary FullAccess key, deeply nested actions or promise structures at the maximum accepted depth, with trailing bytes appended after a valid encoding, reach `to_be_bytes` in `core/primitives-core/src/types.rs` and reach a recursive decode path that exhausts the native stack before any limit applies, breaking the invariant that decoding depth is bounded before recursion, leading to Critical - Network not being able to confirm new transactions (total network shutdown)?

## Target
- File/function: `core/primitives-core/src/types.rs` :: `to_be_bytes`
- Entrypoint: `broadcast_tx_commit` / `send_tx` RPC carrying a SignedTransaction signed by an ordinary FullAccess key
- Attacker controls: deeply nested actions or promise structures at the maximum accepted depth; with trailing bytes appended after a valid encoding
- Exploit idea: reach a recursive decode path that exhausts the native stack before any limit applies
- Invariant to test: decoding depth is bounded before recursion
- Expected Immunefi impact: Critical - Network not being able to confirm new transactions (total network shutdown)
- Fast validation: fuzz test over nesting depth up to and past the limit
