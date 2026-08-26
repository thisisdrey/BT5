# Q4127: signature scheme confusion on the signer key — cost.rs

## Question
Can an unprivileged mainnet account, entering through `broadcast_tx_commit` / `send_tx` RPC carrying a SignedTransaction signed by an ordinary FullAccess key, a secp256k1 key registered where an ed25519 key is expected, and a malleable secp256k1 signature, with the boundary value chosen exactly at the enforced limit, and additionally with the boundary value chosen one unit past the enforced limit, reach `test_value` in `core/parameters/src/cost.rs` and get a signature accepted under a key type whose verification has different malleability properties, breaking the invariant that signature verification binds one key type and rejects malleable variants, leading to High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass?

## Target
- File/function: `core/parameters/src/cost.rs` :: `test_value`
- Entrypoint: `broadcast_tx_commit` / `send_tx` RPC carrying a SignedTransaction signed by an ordinary FullAccess key
- Attacker controls: a secp256k1 key registered where an ed25519 key is expected, and a malleable secp256k1 signature; with the boundary value chosen exactly at the enforced limit; with the boundary value chosen one unit past the enforced limit
- Exploit idea: get a signature accepted under a key type whose verification has different malleability properties
- Invariant to test: signature verification binds one key type and rejects malleable variants
- Expected Immunefi impact: High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass
- Fast validation: unit test feeding a flipped-s secp256k1 signature to transaction verification
