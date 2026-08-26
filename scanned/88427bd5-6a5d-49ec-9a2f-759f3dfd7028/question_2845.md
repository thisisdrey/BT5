# Q2845: recursive structure depth on deserialisation — version.rs

## Question
Can an unprivileged mainnet account, entering through `broadcast_tx_commit` / `send_tx` RPC carrying a SignedTransaction signed by an ordinary FullAccess key, deeply nested actions or promise structures at the maximum accepted depth, with trailing bytes appended after a valid encoding, and additionally with a non-minimal length prefix, reach `clamp_to_supported_protocol_version` in `core/primitives-core/src/version.rs` and reach a recursive decode path that exhausts the native stack before any limit applies, breaking the invariant that decoding depth is bounded before recursion, leading to Critical - Network not being able to confirm new transactions (total network shutdown)?

## Target
- File/function: `core/primitives-core/src/version.rs` :: `clamp_to_supported_protocol_version`
- Entrypoint: `broadcast_tx_commit` / `send_tx` RPC carrying a SignedTransaction signed by an ordinary FullAccess key
- Attacker controls: deeply nested actions or promise structures at the maximum accepted depth; with trailing bytes appended after a valid encoding; with a non-minimal length prefix
- Exploit idea: reach a recursive decode path that exhausts the native stack before any limit applies
- Invariant to test: decoding depth is bounded before recursion
- Expected Immunefi impact: Critical - Network not being able to confirm new transactions (total network shutdown)
- Fast validation: fuzz test over nesting depth up to and past the limit
