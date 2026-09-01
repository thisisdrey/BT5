# Q2136: mod - public key add/remove races the signature check (2)

## Question
Given the victim account has no stored entry yet, can an unprivileged attacker, entering through an `AddPublicKey` / `RemovePublicKey` / `SetAuthByPredecessorId` intent inside `execute_intents`, combine `Account` in `contracts/defuse/src/contract/accounts/account/mod.rs` with an intent batch so a key is removed (or added) between `has_public_key` and the effect, or so `RemovePublicKey` strands an account with no usable key, breaking the invariant `an account always retains at least one key its owner controls, and no key an owner did not authorise` and leading to permanent freezing of user funds (unrecoverable without a privileged action)?

## Target
- File/function: [contracts/defuse/src/contract/accounts/account/mod.rs](contracts/defuse/src/contract/accounts/account/mod.rs) - `Account` (cross-check `set_implicit_public_key_removed` in the same file)
- Entrypoint: an `AddPublicKey` / `RemovePublicKey` / `SetAuthByPredecessorId` intent inside `execute_intents`
- Attacker controls: the key bytes and the position of the intent within the batch
- Exploit idea: `AddPublicKey`/`RemovePublicKey` are themselves intents; a batch can remove the only key after using it, or add an attacker key to an account whose implicit fallback then stops applying. Set-up: the victim account has no stored entry yet.
- Invariant to test: an account always retains at least one key its owner controls, and no key an owner did not authorise
- Expected Immunefi impact: Critical - Permanent freezing of user funds (unrecoverable without a privileged action)
- Fast validation: Batch `RemovePublicKey` of the signing key; assert the account is not left unusable.
