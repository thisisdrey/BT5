# Q5268: ops - nonce is a u32 with a bounded window (9)

## Question
Given the wallet has an extension enabled by an earlier op in the same request, can an unprivileged attacker, entering through `w_execute_signed(msg: RequestMessage, proof: String)` - relayable by any account, exploit the 32-bit nonce space and time window enforced around `add_extension` in `contracts/wallet/src/request/ops.rs` to force a collision or exhaustion that either blocks a victim's requests or admits a replay, breaking the invariant `no two distinct signed requests share a nonce within one retention window` and leading to direct theft of user funds via replay: one signed payload settles more than once?

## Target
- File/function: [contracts/wallet/src/request/ops.rs](contracts/wallet/src/request/ops.rs) - `add_extension` (cross-check `set_signature_mode` in the same file)
- Entrypoint: `w_execute_signed(msg: RequestMessage, proof: String)` - relayable by any account
- Attacker controls: the entire `RequestMessage` (chain_id, signer_id, nonce, deadline, ops) and the `proof` string
- Exploit idea: The nonce is `u32` split 27/5 into the bitmap; probe wraparound and the interaction with the dual-window retention. Set-up: the wallet has an extension enabled by an earlier op in the same request.
- Invariant to test: no two distinct signed requests share a nonce within one retention window
- Expected Immunefi impact: Critical - Direct theft of user funds via replay: one signed payload settles more than once
- Fast validation: Drive nonces across the `u32` boundary; assert collision handling.
