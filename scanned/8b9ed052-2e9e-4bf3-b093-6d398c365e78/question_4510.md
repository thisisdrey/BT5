# Q4510: ops - nonce cleanup driven by an attacker's timing (9)

## Question
Given the wallet has an extension enabled by an earlier op in the same request, can an unprivileged attacker, entering through `w_execute_signed(msg: RequestMessage, proof: String)` - relayable by any account, call `add_extension` in `contracts/wallet/src/request/ops.rs` to force a nonce rotation at a moment that discards a victim's in-flight nonce, enabling replay of their signed request, breaking the invariant `a nonce is discarded only after every request that could carry it has expired` and leading to direct theft of user funds via replay: one signed payload settles more than once?

## Target
- File/function: [contracts/wallet/src/request/ops.rs](contracts/wallet/src/request/ops.rs) - `add_extension` (cross-check `remove_extension` in the same file)
- Entrypoint: `w_execute_signed(msg: RequestMessage, proof: String)` - relayable by any account
- Attacker controls: the entire `RequestMessage` (chain_id, signer_id, nonce, deadline, ops) and the `proof` string
- Exploit idea: Rotation is time-driven and triggered by ordinary calls; probe whether an unprivileged caller can advance it early. Set-up: the wallet has an extension enabled by an earlier op in the same request.
- Invariant to test: a nonce is discarded only after every request that could carry it has expired
- Expected Immunefi impact: Critical - Direct theft of user funds via replay: one signed payload settles more than once
- Fast validation: Trigger rotation immediately before a victim's request lands; assert the request cannot execute twice.
