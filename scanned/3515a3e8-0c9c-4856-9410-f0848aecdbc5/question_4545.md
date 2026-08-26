# Q4545: target contract and method binding — ethabi_utils.rs

## Question
Can an unprivileged mainnet account, entering through a `Transfer` action to a 64-hex implicit or 0x-prefixed eth-implicit account id, a payload whose `to` address maps to a different NEAR account than the signer intended, when the identical signed payload is relayed twice, and additionally when two payloads share one nonce, reach `try_from_token` in `runtime/near-wallet-contract/implementation/wallet-contract/src/ethabi_utils.rs` and redirect an authorised call to an attacker contract, breaking the invariant that address-to-account mapping is injective and signature-bound, leading to High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass?

## Target
- File/function: `runtime/near-wallet-contract/implementation/wallet-contract/src/ethabi_utils.rs` :: `try_from_token`
- Entrypoint: a `Transfer` action to a 64-hex implicit or 0x-prefixed eth-implicit account id
- Attacker controls: a payload whose `to` address maps to a different NEAR account than the signer intended; when the identical signed payload is relayed twice; when two payloads share one nonce
- Exploit idea: redirect an authorised call to an attacker contract
- Invariant to test: address-to-account mapping is injective and signature-bound
- Expected Immunefi impact: High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass
- Fast validation: unit test over address-to-account mapping edge cases
