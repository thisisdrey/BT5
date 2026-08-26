# Q0504: eth ABI decoding of the emulated action payload — error.rs

## Question
Can an unprivileged mainnet account, entering through a `Transfer` action to a 64-hex implicit or 0x-prefixed eth-implicit account id, ABI bytes with offsets pointing outside the payload, oversized dynamic lengths, and duplicated fields, when the recovered address differs from the wallet's stored owner, reach `from` in `runtime/near-wallet-contract/implementation/wallet-contract/src/error.rs` and decode into an action list different from the one the user signed, breaking the invariant that the decoded action list is exactly what the signature covers, leading to High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass?

## Target
- File/function: `runtime/near-wallet-contract/implementation/wallet-contract/src/error.rs` :: `from`
- Entrypoint: a `Transfer` action to a 64-hex implicit or 0x-prefixed eth-implicit account id
- Attacker controls: ABI bytes with offsets pointing outside the payload, oversized dynamic lengths, and duplicated fields; when the recovered address differs from the wallet's stored owner
- Exploit idea: decode into an action list different from the one the user signed
- Invariant to test: the decoded action list is exactly what the signature covers
- Expected Immunefi impact: High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass
- Fast validation: fuzz test comparing signed bytes against decoded actions
