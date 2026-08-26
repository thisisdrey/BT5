# Q4257: enum discriminant beyond the known range — types.rs

## Question
Can an unprivileged mainnet account, entering through `broadcast_tx_commit` / `send_tx` RPC carrying a SignedTransaction signed by an ordinary FullAccess key, an action or receipt enum tag one past the highest defined variant, with a non-minimal length prefix, and additionally with a duplicate or out-of-range enum discriminant, reach `add` in `core/primitives-core/src/types.rs` and make older and newer nodes disagree about whether the payload is valid, breaking the invariant that unknown discriminants are rejected identically on every node at a given protocol version, leading to High - Unintended chain split (network partition)?

## Target
- File/function: `core/primitives-core/src/types.rs` :: `add`
- Entrypoint: `broadcast_tx_commit` / `send_tx` RPC carrying a SignedTransaction signed by an ordinary FullAccess key
- Attacker controls: an action or receipt enum tag one past the highest defined variant; with a non-minimal length prefix; with a duplicate or out-of-range enum discriminant
- Exploit idea: make older and newer nodes disagree about whether the payload is valid
- Invariant to test: unknown discriminants are rejected identically on every node at a given protocol version
- Expected Immunefi impact: High - Unintended chain split (network partition)
- Fast validation: test decoding an out-of-range discriminant under each protocol version
