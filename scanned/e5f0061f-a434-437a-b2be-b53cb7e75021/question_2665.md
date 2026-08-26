# Q2665: borsh non-canonical encoding accepted on the wire — version.rs

## Question
Can an unprivileged mainnet account, entering through `broadcast_tx_commit` / `send_tx` RPC carrying a SignedTransaction signed by an ordinary FullAccess key, an encoding with trailing bytes, a non-minimal length prefix, or a duplicate enum discriminant, with trailing bytes appended after a valid encoding, and additionally with a non-minimal length prefix, reach `clamp_to_supported_protocol_version` in `core/primitives-core/src/version.rs` and have two byte strings deserialise to the same object so the hash and the content disagree, breaking the invariant that deserialisation is canonical: one object has exactly one accepted encoding, leading to Critical - Unintended permanent chain split requiring a hard fork?

## Target
- File/function: `core/primitives-core/src/version.rs` :: `clamp_to_supported_protocol_version`
- Entrypoint: `broadcast_tx_commit` / `send_tx` RPC carrying a SignedTransaction signed by an ordinary FullAccess key
- Attacker controls: an encoding with trailing bytes, a non-minimal length prefix, or a duplicate enum discriminant; with trailing bytes appended after a valid encoding; with a non-minimal length prefix
- Exploit idea: have two byte strings deserialise to the same object so the hash and the content disagree
- Invariant to test: deserialisation is canonical: one object has exactly one accepted encoding
- Expected Immunefi impact: Critical - Unintended permanent chain split requiring a hard fork
- Fast validation: fuzz test asserting re-serialisation equals the accepted input bytes
