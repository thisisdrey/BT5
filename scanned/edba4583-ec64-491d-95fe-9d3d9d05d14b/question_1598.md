# Q1598: account - public key add/remove races the signature check (4)

## Question
Given the victim account has no stored entry yet, can an unprivileged attacker, entering through `execute_intents` mixing payloads from several signers in one vector, combine `execute_intent` in `contracts/defuse/core/src/intents/account.rs` with an intent batch so a key is removed (or added) between `has_public_key` and the effect, or so `RemovePublicKey` strands an account with no usable key, breaking the invariant `an account always retains at least one key its owner controls, and no key an owner did not authorise` and leading to permanent freezing of user funds (unrecoverable without a privileged action)?

## Target
- File/function: [contracts/defuse/core/src/intents/account.rs](contracts/defuse/core/src/intents/account.rs) - `execute_intent` (cross-check `AddPublicKey` in the same file)
- Entrypoint: `execute_intents` mixing payloads from several signers in one vector
- Attacker controls: the number and order of payloads and which accounts each targets
- Exploit idea: `AddPublicKey`/`RemovePublicKey` are themselves intents; a batch can remove the only key after using it, or add an attacker key to an account whose implicit fallback then stops applying. Set-up: the victim account has no stored entry yet.
- Invariant to test: an account always retains at least one key its owner controls, and no key an owner did not authorise
- Expected Immunefi impact: Critical - Permanent freezing of user funds (unrecoverable without a privileged action)
- Fast validation: Batch `RemovePublicKey` of the signing key; assert the account is not left unusable.
