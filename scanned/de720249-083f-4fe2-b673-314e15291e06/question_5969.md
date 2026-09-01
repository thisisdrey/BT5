# Q5969: v0 - storage prefix collision between account sub-maps (37)

## Question
Given the account still has `auth_by_predecessor_id` enabled by default, can an unprivileged attacker, entering through an `AddPublicKey` / `RemovePublicKey` / `SetAuthByPredecessorId` intent inside `execute_intents`, exploit the prefix nesting in `AccountV0` of `contracts/defuse/src/contract/accounts/account/entry/v0.rs` so two different accounts' or tokens' sub-maps share a storage key, letting one account's writes appear in another's balances or nonce bitmap, breaking the invariant `distinct (account, sub-map) pairs map to distinct storage key prefixes` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/defuse/src/contract/accounts/account/entry/v0.rs](contracts/defuse/src/contract/accounts/account/entry/v0.rs) - `AccountV0`
- Entrypoint: an `AddPublicKey` / `RemovePublicKey` / `SetAuthByPredecessorId` intent inside `execute_intents`
- Attacker controls: the key bytes and the position of the intent within the batch
- Exploit idea: `NestPrefix` concatenates a parent prefix with an account id; without a length prefix, `a` + `b.near` can equal `ab` + `.near`. Set-up: the account still has `auth_by_predecessor_id` enabled by default.
- Invariant to test: distinct (account, sub-map) pairs map to distinct storage key prefixes
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Search for account-id pairs producing identical nested prefixes; assert none exist.
