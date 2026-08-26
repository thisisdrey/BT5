# Q0817: target contract and method binding — error.rs

## Question
Can an unprivileged mainnet account, entering through a `Transfer` action to a 64-hex implicit or 0x-prefixed eth-implicit account id, a payload whose `to` address maps to a different NEAR account than the signer intended, when the recovered address differs from the wallet's stored owner, reach `from` in `runtime/near-wallet-contract/implementation/wallet-contract/src/error.rs` and redirect an authorised call to an attacker contract, breaking the invariant that address-to-account mapping is injective and signature-bound, leading to High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass?

## Target
- File/function: `runtime/near-wallet-contract/implementation/wallet-contract/src/error.rs` :: `from`
- Entrypoint: a `Transfer` action to a 64-hex implicit or 0x-prefixed eth-implicit account id
- Attacker controls: a payload whose `to` address maps to a different NEAR account than the signer intended; when the recovered address differs from the wallet's stored owner
- Exploit idea: redirect an authorised call to an attacker contract
- Invariant to test: address-to-account mapping is injective and signature-bound
- Expected Immunefi impact: High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass
- Fast validation: unit test over address-to-account mapping edge cases
