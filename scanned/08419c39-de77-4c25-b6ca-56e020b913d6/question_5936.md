# Q5936: ops - WalletOp ordering lets a request disable its own guard (16)

## Question
Given the wallet is built on the `no-sign` signature schema, can an unprivileged attacker, entering through an on-chain call that triggers nonce rotation or cleanup before a victim's request lands, batch `WalletOp` values through `WalletOp` in `contracts/wallet/src/request/ops.rs` so a `SetSignatureMode`/`AddExtension` op takes effect before later ops in the same request are authorised, breaking the invariant `the authority each op is checked against == the authority in force when the request was signed` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/wallet/src/request/ops.rs](contracts/wallet/src/request/ops.rs) - `WalletOp` (cross-check `enable_signature` in the same file)
- Entrypoint: an on-chain call that triggers nonce rotation or cleanup before a victim's request lands
- Attacker controls: the timing of the triggering call
- Exploit idea: Ops execute in sequence; an op that widens authority applies to the remainder of the same request. Set-up: the wallet is built on the `no-sign` signature schema.
- Invariant to test: the authority each op is checked against == the authority in force when the request was signed
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Submit a request whose first op adds an extension and whose second relies on it; assert the pre-signature authority applies.
