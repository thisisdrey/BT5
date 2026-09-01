# Q4202: account - public key add/remove races the signature check (10)

## Question
Given the victim account is currently locked, can an unprivileged attacker, entering through `simulate_intents` as a probe of another account's state before acting, combine `RemovePublicKey` in `contracts/defuse/core/src/intents/account.rs` with an intent batch so a key is removed (or added) between `has_public_key` and the effect, or so `RemovePublicKey` strands an account with no usable key, breaking the invariant `an account always retains at least one key its owner controls, and no key an owner did not authorise` and leading to permanent freezing of user funds (unrecoverable without a privileged action)?

## Target
- File/function: [contracts/defuse/core/src/intents/account.rs](contracts/defuse/core/src/intents/account.rs) - `RemovePublicKey` (cross-check `SetAuthByPredecessorId` in the same file)
- Entrypoint: `simulate_intents` as a probe of another account's state before acting
- Attacker controls: the probe batch composition
- Exploit idea: `AddPublicKey`/`RemovePublicKey` are themselves intents; a batch can remove the only key after using it, or add an attacker key to an account whose implicit fallback then stops applying. Set-up: the victim account is currently locked.
- Invariant to test: an account always retains at least one key its owner controls, and no key an owner did not authorise
- Expected Immunefi impact: Critical - Permanent freezing of user funds (unrecoverable without a privileged action)
- Fast validation: Batch `RemovePublicKey` of the signing key; assert the account is not left unusable.
