# Q4549: mod - storage prefix collision between account sub-maps (20)

## Question
Given the victim account has no stored entry yet, can an unprivileged attacker, entering through `simulate_intents` as a probe of another account's state before acting, exploit the prefix nesting in `remove_public_key` of `contracts/defuse/src/contract/accounts/mod.rs` so two different accounts' or tokens' sub-maps share a storage key, letting one account's writes appear in another's balances or nonce bitmap, breaking the invariant `distinct (account, sub-map) pairs map to distinct storage key prefixes` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/defuse/src/contract/accounts/mod.rs](contracts/defuse/src/contract/accounts/mod.rs) - `remove_public_key` (cross-check `remove_public_key_and_emit_event` in the same file)
- Entrypoint: `simulate_intents` as a probe of another account's state before acting
- Attacker controls: the probe batch composition
- Exploit idea: `NestPrefix` concatenates a parent prefix with an account id; without a length prefix, `a` + `b.near` can equal `ab` + `.near`. Set-up: the victim account has no stored entry yet.
- Invariant to test: distinct (account, sub-map) pairs map to distinct storage key prefixes
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Search for account-id pairs producing identical nested prefixes; assert none exist.
