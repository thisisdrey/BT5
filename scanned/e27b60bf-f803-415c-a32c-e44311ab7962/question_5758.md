# Q5758: relayer fee handling on a failed emulated action — types.rs

## Question
Can an unprivileged mainnet account, entering through a `Transfer` action to a 64-hex implicit or 0x-prefixed eth-implicit account id, an emulated action that fails after the relayer has been compensated, when two payloads share one nonce, and additionally when the chain id belongs to another network, reach `construct_public_key` in `runtime/near-wallet-contract/implementation/wallet-contract/src/types.rs` and have the wallet pay a relayer without advancing state, enabling repeated drain, breaking the invariant that relayer compensation happens only alongside a nonce advance, leading to High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass?

## Target
- File/function: `runtime/near-wallet-contract/implementation/wallet-contract/src/types.rs` :: `construct_public_key`
- Entrypoint: a `Transfer` action to a 64-hex implicit or 0x-prefixed eth-implicit account id
- Attacker controls: an emulated action that fails after the relayer has been compensated; when two payloads share one nonce; when the chain id belongs to another network
- Exploit idea: have the wallet pay a relayer without advancing state, enabling repeated drain
- Invariant to test: relayer compensation happens only alongside a nonce advance
- Expected Immunefi impact: High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass
- Fast validation: wallet-contract test asserting nonce advance on every paid path
