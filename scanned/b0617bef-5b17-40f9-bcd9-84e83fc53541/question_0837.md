# Q0837: public_key - implicit-account fallback on unregistered account

## Question
Given the victim account has no entry in `self.accounts`, so `has_public_key` falls back to implicit-key derivation, can an unprivileged attacker, entering through `ft_on_transfer` / `mt_on_transfer` `msg` carrying `DepositAction::Execute { execute_intents }` from a token the attacker deployed, exploit the `has_public_key` fallback `account_id == public_key.to_implicit_account_id()` so a payload signed by `PublicKey` in `contracts/defuse/core/src/public_key.rs` authorises an account that has no entry in `self.accounts` but already holds a balance credited by a prior deposit, breaking the invariant `an account authorised by the implicit-key fallback == an account whose funds the key holder provably owns` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/defuse/core/src/public_key.rs](contracts/defuse/core/src/public_key.rs) - `PublicKey` (cross-check `example_secp256k1` in the same file)
- Entrypoint: `ft_on_transfer` / `mt_on_transfer` `msg` carrying `DepositAction::Execute { execute_intents }` from a token the attacker deployed
- Attacker controls: the deposit `msg` string and every `MultiPayload` nested inside it
- Exploit idea: Deposit tokens to a 64-hex implicit `receiver_id` whose account entry is never created, then sign with the matching key — or find a `PublicKey` encoding whose `to_implicit_account_id()` collides with an existing funded account id. Set-up: the victim account has no entry in `self.accounts`, so `has_public_key` falls back to implicit-key derivation.
- Invariant to test: an account authorised by the implicit-key fallback == an account whose funds the key holder provably owns
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Sandbox: `ft_on_transfer` with `msg` naming an implicit id, then `execute_intents` signed by the derived key; assert whether the balance moves without any `add_public_key`.
