# Q2068: schema - no-sign or extension path reachable without authorisation (3)

## Question
Given the wallet's nonce bitmap has just rotated `current` into `old`, can an unprivileged attacker, entering through replay of a `RequestMessage` observed on-chain, against the same or a sibling wallet, reach `verify_offchain_msg` in `contracts/wallet/src/schema.rs` through the `no-sign` signature schema or the extension path so a `Request` executes without any signature check, breaking the invariant `every executed `NearAction` == one the owner signed, or one an owner-enabled extension requested` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/wallet/src/schema.rs](contracts/wallet/src/schema.rs) - `verify_offchain_msg` (cross-check `verify_request_msg` in the same file)
- Entrypoint: replay of a `RequestMessage` observed on-chain, against the same or a sibling wallet
- Attacker controls: when it is replayed and which wallet instance it is sent to
- Exploit idea: `w_execute_extension` trusts `env::predecessor_account_id()` against the enabled-extension set; probe extension enable/disable ordering and the zero-deposit requirement. Set-up: the wallet's nonce bitmap has just rotated `current` into `old`.
- Invariant to test: every executed `NearAction` == one the owner signed, or one an owner-enabled extension requested
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Call `w_execute_extension` from a non-enabled predecessor and with zero deposit; assert rejection.
