# Q3301: access-key management on the wallet account — types.rs

## Question
Can an unprivileged mainnet account, entering through a `Transfer` action to a 64-hex implicit or 0x-prefixed eth-implicit account id, an emulated action list containing AddKey for an attacker key, when the recovered address differs from the wallet's stored owner, and additionally when the identical signed payload is relayed twice, reach `construct_public_key` in `runtime/near-wallet-contract/implementation/wallet-contract/src/types.rs` and install a key on someone else's eth-implicit account, breaking the invariant that the wallet never adds keys outside the protocol-defined lifecycle, leading to High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass?

## Target
- File/function: `runtime/near-wallet-contract/implementation/wallet-contract/src/types.rs` :: `construct_public_key`
- Entrypoint: a `Transfer` action to a 64-hex implicit or 0x-prefixed eth-implicit account id
- Attacker controls: an emulated action list containing AddKey for an attacker key; when the recovered address differs from the wallet's stored owner; when the identical signed payload is relayed twice
- Exploit idea: install a key on someone else's eth-implicit account
- Invariant to test: the wallet never adds keys outside the protocol-defined lifecycle
- Expected Immunefi impact: High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass
- Fast validation: unit test asserting AddKey is unreachable through the emulated path
