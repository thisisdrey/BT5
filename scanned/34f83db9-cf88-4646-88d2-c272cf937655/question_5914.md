# Q5914: state - storage prefix collision between account sub-maps (26)

## Question
Given the account still has `auth_by_predecessor_id` enabled by default, can an unprivileged attacker, entering through `add_public_key` / `remove_public_key` / `disable_auth_by_predecessor_id` called directly (1 yocto, predecessor auth), exploit the prefix nesting in `AccountStatePrefix` of `contracts/defuse/src/contract/accounts/state.rs` so two different accounts' or tokens' sub-maps share a storage key, letting one account's writes appear in another's balances or nonce bitmap, breaking the invariant `distinct (account, sub-map) pairs map to distinct storage key prefixes` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/defuse/src/contract/accounts/state.rs](contracts/defuse/src/contract/accounts/state.rs) - `AccountStatePrefix` (cross-check `AccountState` in the same file)
- Entrypoint: `add_public_key` / `remove_public_key` / `disable_auth_by_predecessor_id` called directly (1 yocto, predecessor auth)
- Attacker controls: the `public_key` argument and the calling account id
- Exploit idea: `NestPrefix` concatenates a parent prefix with an account id; without a length prefix, `a` + `b.near` can equal `ab` + `.near`. Set-up: the account still has `auth_by_predecessor_id` enabled by default.
- Invariant to test: distinct (account, sub-map) pairs map to distinct storage key prefixes
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Search for account-id pairs producing identical nested prefixes; assert none exist.
