# Q0599: lib - chain_id / signer_id binding not enforced (3)

## Question
Given the wallet's nonce bitmap has just rotated `current` into `old`, can an unprivileged attacker, entering through replay of a `RequestMessage` observed on-chain, against the same or a sibling wallet, submit a `RequestMessage` to `sign_extract` in `crates/mpc/signer/src/lib.rs` whose `chain_id` or `signer_id` does not match this network and `env::current_account_id()`, or which was signed for a sibling wallet deployment, breaking the invariant `every executed `Request` names this chain and this wallet account` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [crates/mpc/signer/src/lib.rs](crates/mpc/signer/src/lib.rs) - `sign_extract` (cross-check `derive_sign` in the same file)
- Entrypoint: replay of a `RequestMessage` observed on-chain, against the same or a sibling wallet
- Attacker controls: when it is replayed and which wallet instance it is sent to
- Exploit idea: The interface documents these as MUST-panic conditions; probe whether every implementation and every code path enforces them. Set-up: the wallet's nonce bitmap has just rotated `current` into `old`.
- Invariant to test: every executed `Request` names this chain and this wallet account
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Submit requests with foreign `chain_id` and foreign `signer_id`; assert both panic.
