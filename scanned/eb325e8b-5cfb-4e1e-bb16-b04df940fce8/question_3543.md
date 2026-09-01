# Q3543: lib - MPC tweak / derivation collision across wallets (37)

## Question
Given the wallet's nonce bitmap has just rotated `current` into `old`, can an unprivileged attacker, entering through replay of a `RequestMessage` observed on-chain, against the same or a sibling wallet, find two distinct wallet identities whose derivation through `WalletWebauthnProof` in `contracts/wallet/signatures/webauthn/src/lib.rs` produces the same tweak or derived key, so one wallet can authorise actions for another, breaking the invariant `distinct (account, path) inputs produce distinct derived keys` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/wallet/signatures/webauthn/src/lib.rs](contracts/wallet/signatures/webauthn/src/lib.rs) - `WalletWebauthnProof` (cross-check `verify_request_msg` in the same file)
- Entrypoint: replay of a `RequestMessage` observed on-chain, against the same or a sibling wallet
- Attacker controls: when it is replayed and which wallet instance it is sent to
- Exploit idea: The derivation concatenates identifiers before hashing; unescaped separators or attacker-chosen path components create collisions. Set-up: the wallet's nonce bitmap has just rotated `current` into `old`.
- Invariant to test: distinct (account, path) inputs produce distinct derived keys
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Property-test `WalletWebauthnProof` for collisions with separator-bearing inputs.
