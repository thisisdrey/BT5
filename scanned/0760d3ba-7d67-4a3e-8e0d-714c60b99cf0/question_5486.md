# Q5486: public_key - public-key type confusion across curves (7)

## Question
Given the victim has registered exactly one public key, of a different curve than the one submitted, can an unprivileged attacker, entering through `ft_on_transfer` / `mt_on_transfer` `msg` carrying `DepositAction::Execute { execute_intents }` from a token the attacker deployed, get `example_secp256k1` in `contracts/defuse/core/src/public_key.rs` to accept a `Signature` variant paired with a `PublicKey` variant of a different curve, or to coerce an attacker key into the victim's registered key type, breaking the invariant `the curve of the verified signature == the curve of the `PublicKey` registered on the signer's account` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/defuse/core/src/public_key.rs](contracts/defuse/core/src/public_key.rs) - `example_secp256k1` (cross-check `PublicKey` in the same file)
- Entrypoint: `ft_on_transfer` / `mt_on_transfer` `msg` carrying `DepositAction::Execute { execute_intents }` from a token the attacker deployed
- Attacker controls: the deposit `msg` string and every `MultiPayload` nested inside it
- Exploit idea: Target the match arms pairing `(PublicKey::Ed25519, Signature::Ed25519)` / `(PublicKey::P256, Signature::P256)` and any `try_into().ok()?` that silently discards a malformed key rather than rejecting the payload. Set-up: the victim has registered exactly one public key, of a different curve than the one submitted.
- Invariant to test: the curve of the verified signature == the curve of the `PublicKey` registered on the signer's account
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Unit-test `example_secp256k1` with mismatched key/signature variants and with keys whose `try_into()` fails; assert no arm returns `Some`.
