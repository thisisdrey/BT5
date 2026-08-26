# Q2673: borsh non-canonical encoding accepted on the wire — utils.rs

## Question
Can an unprivileged mainnet account, entering through `broadcast_tx_commit` / `send_tx` RPC carrying a SignedTransaction signed by an ordinary FullAccess key, an encoding with trailing bytes, a non-minimal length prefix, or a duplicate enum discriminant, with trailing bytes appended after a valid encoding, and additionally with a non-minimal length prefix, reach `get_receipt_proof_target_shard_prefix` in `core/primitives/src/utils.rs` and have two byte strings deserialise to the same object so the hash and the content disagree, breaking the invariant that deserialisation is canonical: one object has exactly one accepted encoding, leading to Critical - Unintended permanent chain split requiring a hard fork?

## Target
- File/function: `core/primitives/src/utils.rs` :: `get_receipt_proof_target_shard_prefix`
- Entrypoint: `broadcast_tx_commit` / `send_tx` RPC carrying a SignedTransaction signed by an ordinary FullAccess key
- Attacker controls: an encoding with trailing bytes, a non-minimal length prefix, or a duplicate enum discriminant; with trailing bytes appended after a valid encoding; with a non-minimal length prefix
- Exploit idea: have two byte strings deserialise to the same object so the hash and the content disagree
- Invariant to test: deserialisation is canonical: one object has exactly one accepted encoding
- Expected Immunefi impact: Critical - Unintended permanent chain split requiring a hard fork
- Fast validation: fuzz test asserting re-serialisation equals the accepted input bytes
