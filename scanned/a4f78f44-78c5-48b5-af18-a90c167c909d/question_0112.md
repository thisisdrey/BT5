# Q0112: message - chain_id / signer_id binding not enforced

## Question
Given the wallet's nonce bitmap has just rotated `current` into `old`, can an unprivileged attacker, entering through `w_execute_signed(msg: RequestMessage, proof: String)` - relayable by any account, submit a `RequestMessage` to `OffchainMessage` in `crates/signatures/nep641/src/message.rs` whose `chain_id` or `signer_id` does not match this network and `env::current_account_id()`, or which was signed for a sibling wallet deployment, breaking the invariant `every executed `Request` names this chain and this wallet account` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [crates/signatures/nep641/src/message.rs](crates/signatures/nep641/src/message.rs) - `OffchainMessage` (cross-check `top_level_id` in the same file)
- Entrypoint: `w_execute_signed(msg: RequestMessage, proof: String)` - relayable by any account
- Attacker controls: the entire `RequestMessage` (chain_id, signer_id, nonce, deadline, ops) and the `proof` string
- Exploit idea: The interface documents these as MUST-panic conditions; probe whether every implementation and every code path enforces them. Set-up: the wallet's nonce bitmap has just rotated `current` into `old`.
- Invariant to test: every executed `Request` names this chain and this wallet account
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Submit requests with foreign `chain_id` and foreign `signer_id`; assert both panic.
