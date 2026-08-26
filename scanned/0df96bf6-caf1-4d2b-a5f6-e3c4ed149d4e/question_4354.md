# Q4354: recursive structure depth on deserialisation — state_record.rs

## Question
Can an unprivileged mainnet account, entering through `broadcast_tx_commit` / `send_tx` RPC carrying a SignedTransaction signed by an ordinary FullAccess key, deeply nested actions or promise structures at the maximum accepted depth, with a non-minimal length prefix, and additionally with a duplicate or out-of-range enum discriminant, reach `get_type_string` in `core/primitives/src/state_record.rs` and reach a recursive decode path that exhausts the native stack before any limit applies, breaking the invariant that decoding depth is bounded before recursion, leading to Critical - Network not being able to confirm new transactions (total network shutdown)?

## Target
- File/function: `core/primitives/src/state_record.rs` :: `get_type_string`
- Entrypoint: `broadcast_tx_commit` / `send_tx` RPC carrying a SignedTransaction signed by an ordinary FullAccess key
- Attacker controls: deeply nested actions or promise structures at the maximum accepted depth; with a non-minimal length prefix; with a duplicate or out-of-range enum discriminant
- Exploit idea: reach a recursive decode path that exhausts the native stack before any limit applies
- Invariant to test: decoding depth is bounded before recursion
- Expected Immunefi impact: Critical - Network not being able to confirm new transactions (total network shutdown)
- Fast validation: fuzz test over nesting depth up to and past the limit
