# Q4129: nonces - MPC tweak / derivation collision across wallets (8)

## Question
Given the request is replayed against a sibling subwallet deployment, can an unprivileged attacker, entering through an on-chain call that triggers nonce rotation or cleanup before a victim's request lands, find two distinct wallet identities whose derivation through `timeout` in `contracts/wallet/src/nonces.rs` produces the same tweak or derived key, so one wallet can authorise actions for another, breaking the invariant `distinct (account, path) inputs produce distinct derived keys` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/wallet/src/nonces.rs](contracts/wallet/src/nonces.rs) - `timeout` (cross-check `Nonces` in the same file)
- Entrypoint: an on-chain call that triggers nonce rotation or cleanup before a victim's request lands
- Attacker controls: the timing of the triggering call
- Exploit idea: The derivation concatenates identifiers before hashing; unescaped separators or attacker-chosen path components create collisions. Set-up: the request is replayed against a sibling subwallet deployment.
- Invariant to test: distinct (account, path) inputs produce distinct derived keys
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Property-test `timeout` for collisions with separator-bearing inputs.
