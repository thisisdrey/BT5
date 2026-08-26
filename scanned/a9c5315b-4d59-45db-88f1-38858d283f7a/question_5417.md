# Q5417: borsh non-canonical encoding accepted on the wire — upgrade_schedule.rs

## Question
Can an unprivileged mainnet account, entering through `broadcast_tx_commit` / `send_tx` RPC carrying a SignedTransaction signed by an ordinary FullAccess key, an encoding with trailing bytes, a non-minimal length prefix, or a duplicate enum discriminant, with a duplicate or out-of-range enum discriminant, and additionally with nesting at exactly the maximum accepted depth, reach `protocol_version_to_vote_for_at_date` in `core/primitives/src/upgrade_schedule.rs` and have two byte strings deserialise to the same object so the hash and the content disagree, breaking the invariant that deserialisation is canonical: one object has exactly one accepted encoding, leading to Critical - Unintended permanent chain split requiring a hard fork?

## Target
- File/function: `core/primitives/src/upgrade_schedule.rs` :: `protocol_version_to_vote_for_at_date`
- Entrypoint: `broadcast_tx_commit` / `send_tx` RPC carrying a SignedTransaction signed by an ordinary FullAccess key
- Attacker controls: an encoding with trailing bytes, a non-minimal length prefix, or a duplicate enum discriminant; with a duplicate or out-of-range enum discriminant; with nesting at exactly the maximum accepted depth
- Exploit idea: have two byte strings deserialise to the same object so the hash and the content disagree
- Invariant to test: deserialisation is canonical: one object has exactly one accepted encoding
- Expected Immunefi impact: Critical - Unintended permanent chain split requiring a hard fork
- Fast validation: fuzz test asserting re-serialisation equals the accepted input bytes
