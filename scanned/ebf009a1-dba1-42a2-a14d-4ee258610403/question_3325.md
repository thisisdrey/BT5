# Q3325: access_keys - chain_id / signer_id binding not enforced (5)

## Question
Given the request is replayed against a sibling subwallet deployment, can an unprivileged attacker, entering through `w_execute_signed(msg: RequestMessage, proof: String)` - relayable by any account, submit a `RequestMessage` to `resolve_access_key` in `crates/signatures/nep641/src/resolver/access_keys.rs` whose `chain_id` or `signer_id` does not match this network and `env::current_account_id()`, or which was signed for a sibling wallet deployment, breaking the invariant `every executed `Request` names this chain and this wallet account` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [crates/signatures/nep641/src/resolver/access_keys.rs](crates/signatures/nep641/src/resolver/access_keys.rs) - `resolve_access_key` (cross-check `AccessKeyError` in the same file)
- Entrypoint: `w_execute_signed(msg: RequestMessage, proof: String)` - relayable by any account
- Attacker controls: the entire `RequestMessage` (chain_id, signer_id, nonce, deadline, ops) and the `proof` string
- Exploit idea: The interface documents these as MUST-panic conditions; probe whether every implementation and every code path enforces them. Set-up: the request is replayed against a sibling subwallet deployment.
- Invariant to test: every executed `Request` names this chain and this wallet account
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Submit requests with foreign `chain_id` and foreign `signer_id`; assert both panic.
