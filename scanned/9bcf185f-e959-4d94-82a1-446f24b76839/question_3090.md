# Q3090: target contract and method binding — internal.rs

## Question
Can an unprivileged mainnet account, entering through a `Transfer` action to a 64-hex implicit or 0x-prefixed eth-implicit account id, a payload whose `to` address maps to a different NEAR account than the signer intended, when the recovered address differs from the wallet's stored owner, and additionally when the identical signed payload is relayed twice, reach `parse_rlp_tx_to_action` in `runtime/near-wallet-contract/implementation/wallet-contract/src/internal.rs` and redirect an authorised call to an attacker contract, breaking the invariant that address-to-account mapping is injective and signature-bound, leading to High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass?

## Target
- File/function: `runtime/near-wallet-contract/implementation/wallet-contract/src/internal.rs` :: `parse_rlp_tx_to_action`
- Entrypoint: a `Transfer` action to a 64-hex implicit or 0x-prefixed eth-implicit account id
- Attacker controls: a payload whose `to` address maps to a different NEAR account than the signer intended; when the recovered address differs from the wallet's stored owner; when the identical signed payload is relayed twice
- Exploit idea: redirect an authorised call to an attacker contract
- Invariant to test: address-to-account mapping is injective and signature-bound
- Expected Immunefi impact: High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass
- Fast validation: unit test over address-to-account mapping edge cases
