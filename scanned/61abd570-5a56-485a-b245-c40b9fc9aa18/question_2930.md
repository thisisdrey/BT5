# Q2930: implicit account creation via Transfer to a 64-hex id — account.rs

## Question
Can an unprivileged mainnet account, entering through a `Transfer` action to a 64-hex implicit or 0x-prefixed eth-implicit account id, a 64-hex account id chosen so that it collides with an ed25519 public key the attacker also controls, when combined with a DeployContract earlier in the same action list, and additionally when combined with a DeleteAccount later in the same action list, reach `uninitialized_account_from_serde` in `core/primitives-core/src/account.rs` and create the implicit account with a key or state that the transfer path did not authorise, breaking the invariant that an implicit account is only ever controlled by the key its id is derived from, leading to High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass?

## Target
- File/function: `core/primitives-core/src/account.rs` :: `uninitialized_account_from_serde`
- Entrypoint: a `Transfer` action to a 64-hex implicit or 0x-prefixed eth-implicit account id
- Attacker controls: a 64-hex account id chosen so that it collides with an ed25519 public key the attacker also controls; when combined with a DeployContract earlier in the same action list; when combined with a DeleteAccount later in the same action list
- Exploit idea: create the implicit account with a key or state that the transfer path did not authorise
- Invariant to test: an implicit account is only ever controlled by the key its id is derived from
- Expected Immunefi impact: High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass
- Fast validation: unit test on implicit-account derivation asserting id/key correspondence
