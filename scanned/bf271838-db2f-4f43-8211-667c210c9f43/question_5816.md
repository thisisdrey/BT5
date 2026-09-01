# Q5816: ops - WalletOp ordering lets a request disable its own guard (14)

## Question
Given the wallet is built on the `no-sign` signature schema, can an unprivileged attacker, entering through `w_execute_extension(request: Request)` called from any predecessor with a non-zero deposit, batch `WalletOp` values through `enable_signature` in `contracts/wallet/src/request/ops.rs` so a `SetSignatureMode`/`AddExtension` op takes effect before later ops in the same request are authorised, breaking the invariant `the authority each op is checked against == the authority in force when the request was signed` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/wallet/src/request/ops.rs](contracts/wallet/src/request/ops.rs) - `enable_signature` (cross-check `set_signature_mode` in the same file)
- Entrypoint: `w_execute_extension(request: Request)` called from any predecessor with a non-zero deposit
- Attacker controls: the `Request` contents, the calling account id, and the attached deposit
- Exploit idea: Ops execute in sequence; an op that widens authority applies to the remainder of the same request. Set-up: the wallet is built on the `no-sign` signature schema.
- Invariant to test: the authority each op is checked against == the authority in force when the request was signed
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Submit a request whose first op adds an extension and whose second relies on it; assert the pre-signature authority applies.
