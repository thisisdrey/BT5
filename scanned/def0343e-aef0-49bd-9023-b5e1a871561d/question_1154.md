# Q1154: access-key management on the wallet account — ethabi_utils.rs

## Question
Can an unprivileged mainnet account, entering through a `Transfer` action to a 64-hex implicit or 0x-prefixed eth-implicit account id, an emulated action list containing AddKey for an attacker key, when the recovered address differs from the wallet's stored owner, reach `abi_decode` in `runtime/near-wallet-contract/implementation/wallet-contract/src/ethabi_utils.rs` and install a key on someone else's eth-implicit account, breaking the invariant that the wallet never adds keys outside the protocol-defined lifecycle, leading to High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass?

## Target
- File/function: `runtime/near-wallet-contract/implementation/wallet-contract/src/ethabi_utils.rs` :: `abi_decode`
- Entrypoint: a `Transfer` action to a 64-hex implicit or 0x-prefixed eth-implicit account id
- Attacker controls: an emulated action list containing AddKey for an attacker key; when the recovered address differs from the wallet's stored owner
- Exploit idea: install a key on someone else's eth-implicit account
- Invariant to test: the wallet never adds keys outside the protocol-defined lifecycle
- Expected Immunefi impact: High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass
- Fast validation: unit test asserting AddKey is unreachable through the emulated path
