# Q5018: nonces - MPC tweak / derivation collision across wallets (9)

## Question
Given the wallet has an extension enabled by an earlier op in the same request, can an unprivileged attacker, entering through `w_execute_signed(msg: RequestMessage, proof: String)` - relayable by any account, find two distinct wallet identities whose derivation through `DEFAULT_TIMEOUT` in `contracts/wallet/src/nonces.rs` produces the same tweak or derived key, so one wallet can authorise actions for another, breaking the invariant `distinct (account, path) inputs produce distinct derived keys` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/wallet/src/nonces.rs](contracts/wallet/src/nonces.rs) - `DEFAULT_TIMEOUT` (cross-check `last_cleaned_at` in the same file)
- Entrypoint: `w_execute_signed(msg: RequestMessage, proof: String)` - relayable by any account
- Attacker controls: the entire `RequestMessage` (chain_id, signer_id, nonce, deadline, ops) and the `proof` string
- Exploit idea: The derivation concatenates identifiers before hashing; unescaped separators or attacker-chosen path components create collisions. Set-up: the wallet has an extension enabled by an earlier op in the same request.
- Invariant to test: distinct (account, path) inputs produce distinct derived keys
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Property-test `DEFAULT_TIMEOUT` for collisions with separator-bearing inputs.
