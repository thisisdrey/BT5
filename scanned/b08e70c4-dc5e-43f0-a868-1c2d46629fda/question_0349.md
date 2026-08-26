# Q0349: chain id binding on the emulated eth transaction — error.rs

## Question
Can an unprivileged mainnet account, entering through a `Transfer` action to a 64-hex implicit or 0x-prefixed eth-implicit account id, a signed payload whose chain id belongs to another network, when the recovered address differs from the wallet's stored owner, reach `from` in `runtime/near-wallet-contract/implementation/wallet-contract/src/error.rs` and replay a signature captured elsewhere against the NEAR wallet contract, breaking the invariant that signature verification binds the NEAR chain id, leading to High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass?

## Target
- File/function: `runtime/near-wallet-contract/implementation/wallet-contract/src/error.rs` :: `from`
- Entrypoint: a `Transfer` action to a 64-hex implicit or 0x-prefixed eth-implicit account id
- Attacker controls: a signed payload whose chain id belongs to another network; when the recovered address differs from the wallet's stored owner
- Exploit idea: replay a signature captured elsewhere against the NEAR wallet contract
- Invariant to test: signature verification binds the NEAR chain id
- Expected Immunefi impact: High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass
- Fast validation: unit test with a foreign chain id asserting rejection
