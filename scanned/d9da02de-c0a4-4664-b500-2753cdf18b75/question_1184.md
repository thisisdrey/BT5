# Q1184: convert - MPC tweak / derivation collision across wallets (2)

## Question
Given the wallet's nonce bitmap has just rotated `current` into `old`, can an unprivileged attacker, entering through `w_execute_extension(request: Request)` called from any predecessor with a non-zero deposit, find two distinct wallet identities whose derivation through `RecoverableOnChainNearMpcCurve` in `crates/mpc/signer/src/convert.rs` produces the same tweak or derived key, so one wallet can authorise actions for another, breaking the invariant `distinct (account, path) inputs produce distinct derived keys` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [crates/mpc/signer/src/convert.rs](crates/mpc/signer/src/convert.rs) - `RecoverableOnChainNearMpcCurve` (cross-check `OnChainNearMpcCurve` in the same file)
- Entrypoint: `w_execute_extension(request: Request)` called from any predecessor with a non-zero deposit
- Attacker controls: the `Request` contents, the calling account id, and the attached deposit
- Exploit idea: The derivation concatenates identifiers before hashing; unescaped separators or attacker-chosen path components create collisions. Set-up: the wallet's nonce bitmap has just rotated `current` into `old`.
- Invariant to test: distinct (account, path) inputs produce distinct derived keys
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Property-test `RecoverableOnChainNearMpcCurve` for collisions with separator-bearing inputs.
