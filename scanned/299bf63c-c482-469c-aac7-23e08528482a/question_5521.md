# Q5521: chain id binding on the emulated eth transaction — ethabi_utils.rs

## Question
Can an unprivileged mainnet account, entering through a `Transfer` action to a 64-hex implicit or 0x-prefixed eth-implicit account id, a signed payload whose chain id belongs to another network, when two payloads share one nonce, and additionally when the chain id belongs to another network, reach `data_to_tokens` in `runtime/near-wallet-contract/implementation/wallet-contract/src/ethabi_utils.rs` and replay a signature captured elsewhere against the NEAR wallet contract, breaking the invariant that signature verification binds the NEAR chain id, leading to High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass?

## Target
- File/function: `runtime/near-wallet-contract/implementation/wallet-contract/src/ethabi_utils.rs` :: `data_to_tokens`
- Entrypoint: a `Transfer` action to a 64-hex implicit or 0x-prefixed eth-implicit account id
- Attacker controls: a signed payload whose chain id belongs to another network; when two payloads share one nonce; when the chain id belongs to another network
- Exploit idea: replay a signature captured elsewhere against the NEAR wallet contract
- Invariant to test: signature verification binds the NEAR chain id
- Expected Immunefi impact: High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass
- Fast validation: unit test with a foreign chain id asserting rejection
