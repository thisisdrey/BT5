# Q3515: mod - WalletOp ordering lets a request disable its own guard (3)

## Question
Given the wallet's nonce bitmap has just rotated `current` into `old`, can an unprivileged attacker, entering through replay of a `RequestMessage` observed on-chain, against the same or a sibling wallet, batch `WalletOp` values through `extend` in `contracts/wallet/src/request/mod.rs` so a `SetSignatureMode`/`AddExtension` op takes effect before later ops in the same request are authorised, breaking the invariant `the authority each op is checked against == the authority in force when the request was signed` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/wallet/src/request/mod.rs](contracts/wallet/src/request/mod.rs) - `extend` (cross-check `PER_INTERNAL_OP` in the same file)
- Entrypoint: replay of a `RequestMessage` observed on-chain, against the same or a sibling wallet
- Attacker controls: when it is replayed and which wallet instance it is sent to
- Exploit idea: Ops execute in sequence; an op that widens authority applies to the remainder of the same request. Set-up: the wallet's nonce bitmap has just rotated `current` into `old`.
- Invariant to test: the authority each op is checked against == the authority in force when the request was signed
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Submit a request whose first op adds an extension and whose second relies on it; assert the pre-signature authority applies.
