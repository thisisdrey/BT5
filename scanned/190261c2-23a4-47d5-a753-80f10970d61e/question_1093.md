# Q1093: schema - chain_id / signer_id binding not enforced (2)

## Question
Given the wallet's nonce bitmap has just rotated `current` into `old`, can an unprivileged attacker, entering through `w_execute_extension(request: Request)` called from any predecessor with a non-zero deposit, submit a `RequestMessage` to `SignatureSchema` in `contracts/wallet/src/schema.rs` whose `chain_id` or `signer_id` does not match this network and `env::current_account_id()`, or which was signed for a sibling wallet deployment, breaking the invariant `every executed `Request` names this chain and this wallet account` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/wallet/src/schema.rs](contracts/wallet/src/schema.rs) - `SignatureSchema` (cross-check `verify_request_msg` in the same file)
- Entrypoint: `w_execute_extension(request: Request)` called from any predecessor with a non-zero deposit
- Attacker controls: the `Request` contents, the calling account id, and the attached deposit
- Exploit idea: The interface documents these as MUST-panic conditions; probe whether every implementation and every code path enforces them. Set-up: the wallet's nonce bitmap has just rotated `current` into `old`.
- Invariant to test: every executed `Request` names this chain and this wallet account
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Submit requests with foreign `chain_id` and foreign `signer_id`; assert both panic.
