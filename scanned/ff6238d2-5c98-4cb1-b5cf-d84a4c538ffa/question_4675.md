# Q4675: mod - public key add/remove races the signature check (11)

## Question
Given the victim account is currently locked, can an unprivileged attacker, entering through `add_public_key` / `remove_public_key` / `disable_auth_by_predecessor_id` called directly (1 yocto, predecessor auth), combine `Account` in `contracts/defuse/src/contract/accounts/account/mod.rs` with an intent batch so a key is removed (or added) between `has_public_key` and the effect, or so `RemovePublicKey` strands an account with no usable key, breaking the invariant `an account always retains at least one key its owner controls, and no key an owner did not authorise` and leading to permanent freezing of user funds (unrecoverable without a privileged action)?

## Target
- File/function: [contracts/defuse/src/contract/accounts/account/mod.rs](contracts/defuse/src/contract/accounts/account/mod.rs) - `Account` (cross-check `is_implicit_public_key_removed` in the same file)
- Entrypoint: `add_public_key` / `remove_public_key` / `disable_auth_by_predecessor_id` called directly (1 yocto, predecessor auth)
- Attacker controls: the `public_key` argument and the calling account id
- Exploit idea: `AddPublicKey`/`RemovePublicKey` are themselves intents; a batch can remove the only key after using it, or add an attacker key to an account whose implicit fallback then stops applying. Set-up: the victim account is currently locked.
- Invariant to test: an account always retains at least one key its owner controls, and no key an owner did not authorise
- Expected Immunefi impact: Critical - Permanent freezing of user funds (unrecoverable without a privileged action)
- Fast validation: Batch `RemovePublicKey` of the signing key; assert the account is not left unusable.
