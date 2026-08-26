# Q3383: protocol feature gating derived from a version comparison — version.rs

## Question
Can an unprivileged mainnet account, entering through `broadcast_tx_commit` / `send_tx` RPC carrying a SignedTransaction signed by an ordinary FullAccess key, activity spanning the exact block where a feature's version predicate flips, with trailing bytes appended after a valid encoding, and additionally with a non-minimal length prefix, reach `protocol_version` in `core/primitives-core/src/version.rs` and have two nodes evaluate the predicate differently for the same block, breaking the invariant that feature activation is a total, deterministic function of the block's protocol version, leading to Critical - Unintended permanent chain split requiring a hard fork?

## Target
- File/function: `core/primitives-core/src/version.rs` :: `protocol_version`
- Entrypoint: `broadcast_tx_commit` / `send_tx` RPC carrying a SignedTransaction signed by an ordinary FullAccess key
- Attacker controls: activity spanning the exact block where a feature's version predicate flips; with trailing bytes appended after a valid encoding; with a non-minimal length prefix
- Exploit idea: have two nodes evaluate the predicate differently for the same block
- Invariant to test: feature activation is a total, deterministic function of the block's protocol version
- Expected Immunefi impact: Critical - Unintended permanent chain split requiring a hard fork
- Fast validation: test evaluating every feature predicate at the activation height
