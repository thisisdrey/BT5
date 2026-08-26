# Q0517: length-prefixed collection allocation before validation — types.rs

## Question
Can an unprivileged mainnet account, entering through `broadcast_tx_commit` / `send_tx` RPC carrying a SignedTransaction signed by an ordinary FullAccess key, a collection length field far larger than the payload that follows, with trailing bytes appended after a valid encoding, reach `add` in `core/primitives-core/src/types.rs` and make the decoder commit to work proportional to the declared length rather than the real bytes, breaking the invariant that declared lengths are validated against remaining input before use, leading to Critical - Network not being able to confirm new transactions (total network shutdown)?

## Target
- File/function: `core/primitives-core/src/types.rs` :: `add`
- Entrypoint: `broadcast_tx_commit` / `send_tx` RPC carrying a SignedTransaction signed by an ordinary FullAccess key
- Attacker controls: a collection length field far larger than the payload that follows; with trailing bytes appended after a valid encoding
- Exploit idea: make the decoder commit to work proportional to the declared length rather than the real bytes
- Invariant to test: declared lengths are validated against remaining input before use
- Expected Immunefi impact: Critical - Network not being able to confirm new transactions (total network shutdown)
- Fast validation: fuzz test with oversized length prefixes and truncated payloads
