# Q5632: eth-implicit account transfer path — universal_state_init.rs

## Question
Can an unprivileged mainnet account, entering through a `Transfer` action to a 64-hex implicit or 0x-prefixed eth-implicit account id, a 0x-prefixed eth-implicit id with mixed-case hex and an address that also parses as a named account, when the receiver account already exists with balance and keys, and additionally when the receiver account does not yet exist, reach `code` in `core/primitives/src/universal_state_init.rs` and reach a path where the eth-implicit account is initialised with the wallet contract but a mismatched address, breaking the invariant that the eth-implicit account id, its stored address, and the wallet-contract owner always agree, leading to High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass?

## Target
- File/function: `core/primitives/src/universal_state_init.rs` :: `code`
- Entrypoint: a `Transfer` action to a 64-hex implicit or 0x-prefixed eth-implicit account id
- Attacker controls: a 0x-prefixed eth-implicit id with mixed-case hex and an address that also parses as a named account; when the receiver account already exists with balance and keys; when the receiver account does not yet exist
- Exploit idea: reach a path where the eth-implicit account is initialised with the wallet contract but a mismatched address
- Invariant to test: the eth-implicit account id, its stored address, and the wallet-contract owner always agree
- Expected Immunefi impact: High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass
- Fast validation: unit test comparing derived address against the deployed wallet contract's stored owner
