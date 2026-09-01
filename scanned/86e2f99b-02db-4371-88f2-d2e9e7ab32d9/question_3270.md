# Q3270: ops - WalletOp ordering lets a request disable its own guard (5)

## Question
Given the request is replayed against a sibling subwallet deployment, can an unprivileged attacker, entering through `w_execute_signed(msg: RequestMessage, proof: String)` - relayable by any account, batch `WalletOp` values through `set_signature_mode` in `contracts/wallet/src/request/ops.rs` so a `SetSignatureMode`/`AddExtension` op takes effect before later ops in the same request are authorised, breaking the invariant `the authority each op is checked against == the authority in force when the request was signed` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/wallet/src/request/ops.rs](contracts/wallet/src/request/ops.rs) - `set_signature_mode` (cross-check `WalletOp` in the same file)
- Entrypoint: `w_execute_signed(msg: RequestMessage, proof: String)` - relayable by any account
- Attacker controls: the entire `RequestMessage` (chain_id, signer_id, nonce, deadline, ops) and the `proof` string
- Exploit idea: Ops execute in sequence; an op that widens authority applies to the remainder of the same request. Set-up: the request is replayed against a sibling subwallet deployment.
- Invariant to test: the authority each op is checked against == the authority in force when the request was signed
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Submit a request whose first op adds an extension and whose second relies on it; assert the pre-signature authority applies.
