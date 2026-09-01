# Q4741: mod - Lock serialization skips the flag (20)

## Question
Given the victim account has no stored entry yet, can an unprivileged attacker, entering through `ft_on_transfer` with a `msg` naming any `receiver_id`, which force-creates that account entry, exploit that `Lock<T>` in `contracts/defuse/src/contract/accounts/mod.rs` serialises `locked` with `skip_serializing_if = Not::not`, so a round-trip through `ensure_auth_predecessor_id` (state read/write, ABI, or a migration) loses the locked flag, breaking the invariant `an account's `is_locked` state after any serialisation round-trip == its state before` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/defuse/src/contract/accounts/mod.rs](contracts/defuse/src/contract/accounts/mod.rs) - `ensure_auth_predecessor_id` (cross-check `is_nonce_used` in the same file)
- Entrypoint: `ft_on_transfer` with a `msg` naming any `receiver_id`, which force-creates that account entry
- Attacker controls: the target `receiver_id` and the (possibly minimal) deposited amount
- Exploit idea: A dropped `locked: true` silently unlocks a frozen account. Set-up: the victim account has no stored entry yet.
- Invariant to test: an account's `is_locked` state after any serialisation round-trip == its state before
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Round-trip a locked `Lock<Account>` through borsh and serde; assert the flag survives.
