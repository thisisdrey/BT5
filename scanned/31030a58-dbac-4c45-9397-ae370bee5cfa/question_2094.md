# Q2094: message - AuthResolver resolution accepted for the wrong signer (3)

## Question
Given the wallet's nonce bitmap has just rotated `current` into `old`, can an unprivileged attacker, entering through replay of a `RequestMessage` observed on-chain, against the same or a sibling wallet, make `top_level_id` in `crates/signatures/nep641/src/message.rs` return an `AuthorizationResolution` that authorises a `signer_id` other than the one the resolved access key or contract actually belongs to, breaking the invariant `the `signer_id` an `AuthorizationResolution` authorises == the account whose key was actually resolved` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [crates/signatures/nep641/src/message.rs](crates/signatures/nep641/src/message.rs) - `top_level_id` (cross-check `OffchainMessage` in the same file)
- Entrypoint: replay of a `RequestMessage` observed on-chain, against the same or a sibling wallet
- Attacker controls: when it is replayed and which wallet instance it is sent to
- Exploit idea: NEP-641 resolution maps an account/key to an authorisation decision; probe caching, stale key sets, and default-allow branches. Set-up: the wallet's nonce bitmap has just rotated `current` into `old`.
- Invariant to test: the `signer_id` an `AuthorizationResolution` authorises == the account whose key was actually resolved
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Resolve against a rotated/removed key; assert authorisation is refused.
