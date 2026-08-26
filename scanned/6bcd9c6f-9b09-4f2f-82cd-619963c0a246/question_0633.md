# Q0633: value and gas fields on the emulated call — internal.rs

## Question
Can an unprivileged mainnet account, entering through a `Transfer` action to a 64-hex implicit or 0x-prefixed eth-implicit account id, a payload whose value field and the NEAR deposit it emulates disagree by scaling or truncation, when the recovered address differs from the wallet's stored owner, reach `parse_rlp_tx_to_action` in `runtime/near-wallet-contract/implementation/wallet-contract/src/internal.rs` and move more NEAR than the signed eth transaction authorised, breaking the invariant that the emulated deposit equals the signed value under the fixed conversion, leading to High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass?

## Target
- File/function: `runtime/near-wallet-contract/implementation/wallet-contract/src/internal.rs` :: `parse_rlp_tx_to_action`
- Entrypoint: a `Transfer` action to a 64-hex implicit or 0x-prefixed eth-implicit account id
- Attacker controls: a payload whose value field and the NEAR deposit it emulates disagree by scaling or truncation; when the recovered address differs from the wallet's stored owner
- Exploit idea: move more NEAR than the signed eth transaction authorised
- Invariant to test: the emulated deposit equals the signed value under the fixed conversion
- Expected Immunefi impact: High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass
- Fast validation: unit test comparing signed value to the generated Transfer amount
