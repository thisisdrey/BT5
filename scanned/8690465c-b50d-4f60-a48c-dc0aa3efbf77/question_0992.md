# Q0992: secp256k1 - MPC tweak / derivation collision across wallets (3)

## Question
Given the wallet's nonce bitmap has just rotated `current` into `old`, can an unprivileged attacker, entering through `w_execute_signed(msg: RequestMessage, proof: String)` - relayable by any account, find two distinct wallet identities whose derivation through `parse_recoverable_signature` in `crates/mpc/signer/src/secp256k1.rs` produces the same tweak or derived key, so one wallet can authorise actions for another, breaking the invariant `distinct (account, path) inputs produce distinct derived keys` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [crates/mpc/signer/src/secp256k1.rs](crates/mpc/signer/src/secp256k1.rs) - `parse_recoverable_signature` (cross-check `extract_signature` in the same file)
- Entrypoint: `w_execute_signed(msg: RequestMessage, proof: String)` - relayable by any account
- Attacker controls: the entire `RequestMessage` (chain_id, signer_id, nonce, deadline, ops) and the `proof` string
- Exploit idea: The derivation concatenates identifiers before hashing; unescaped separators or attacker-chosen path components create collisions. Set-up: the wallet's nonce bitmap has just rotated `current` into `old`.
- Invariant to test: distinct (account, path) inputs produce distinct derived keys
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Property-test `parse_recoverable_signature` for collisions with separator-bearing inputs.
