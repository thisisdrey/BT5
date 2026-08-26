# Q5354: signature scheme confusion on the signer key — transaction.rs

## Question
Can an unprivileged mainnet account, entering through `broadcast_tx_commit` / `send_tx` RPC carrying a SignedTransaction signed by an ordinary FullAccess key, a secp256k1 key registered where an ed25519 key is expected, and a malleable secp256k1 signature, with the boundary value chosen one unit past the enforced limit, and additionally when the same input is submitted through two RPC nodes in the same block height, reach `signer_id` in `core/primitives/src/transaction.rs` and get a signature accepted under a key type whose verification has different malleability properties, breaking the invariant that signature verification binds one key type and rejects malleable variants, leading to High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass?

## Target
- File/function: `core/primitives/src/transaction.rs` :: `signer_id`
- Entrypoint: `broadcast_tx_commit` / `send_tx` RPC carrying a SignedTransaction signed by an ordinary FullAccess key
- Attacker controls: a secp256k1 key registered where an ed25519 key is expected, and a malleable secp256k1 signature; with the boundary value chosen one unit past the enforced limit; when the same input is submitted through two RPC nodes in the same block height
- Exploit idea: get a signature accepted under a key type whose verification has different malleability properties
- Invariant to test: signature verification binds one key type and rejects malleable variants
- Expected Immunefi impact: High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass
- Fast validation: unit test feeding a flipped-s secp256k1 signature to transaction verification
