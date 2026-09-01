# Q1369: secp256k1 - MPC tweak / derivation collision across wallets (7)

## Question
Given the wallet's nonce bitmap has just rotated `current` into `old`, can an unprivileged attacker, entering through replay of a `RequestMessage` observed on-chain, against the same or a sibling wallet, find two distinct wallet identities whose derivation through `derive_sign_recoverable` in `crates/kdf/src/secp256k1.rs` produces the same tweak or derived key, so one wallet can authorise actions for another, breaking the invariant `distinct (account, path) inputs produce distinct derived keys` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [crates/kdf/src/secp256k1.rs](crates/kdf/src/secp256k1.rs) - `derive_sign_recoverable` (cross-check `derive_sign` in the same file)
- Entrypoint: replay of a `RequestMessage` observed on-chain, against the same or a sibling wallet
- Attacker controls: when it is replayed and which wallet instance it is sent to
- Exploit idea: The derivation concatenates identifiers before hashing; unescaped separators or attacker-chosen path components create collisions. Set-up: the wallet's nonce bitmap has just rotated `current` into `old`.
- Invariant to test: distinct (account, path) inputs produce distinct derived keys
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Property-test `derive_sign_recoverable` for collisions with separator-bearing inputs.
