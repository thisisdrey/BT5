# Q4766: ops - WalletOp ordering lets a request disable its own guard (9)

## Question
Given the wallet has an extension enabled by an earlier op in the same request, can an unprivileged attacker, entering through `w_execute_signed(msg: RequestMessage, proof: String)` - relayable by any account, batch `WalletOp` values through `add_extension` in `contracts/wallet/src/request/ops.rs` so a `SetSignatureMode`/`AddExtension` op takes effect before later ops in the same request are authorised, breaking the invariant `the authority each op is checked against == the authority in force when the request was signed` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/wallet/src/request/ops.rs](contracts/wallet/src/request/ops.rs) - `add_extension` (cross-check `WalletOp` in the same file)
- Entrypoint: `w_execute_signed(msg: RequestMessage, proof: String)` - relayable by any account
- Attacker controls: the entire `RequestMessage` (chain_id, signer_id, nonce, deadline, ops) and the `proof` string
- Exploit idea: Ops execute in sequence; an op that widens authority applies to the remainder of the same request. Set-up: the wallet has an extension enabled by an earlier op in the same request.
- Invariant to test: the authority each op is checked against == the authority in force when the request was signed
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Submit a request whose first op adds an extension and whose second relies on it; assert the pre-signature authority applies.
