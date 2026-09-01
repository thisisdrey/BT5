# Q5391: mod - no-sign or extension path reachable without authorisation (7)

## Question
Given the request is replayed against a sibling subwallet deployment, can an unprivileged attacker, entering through replay of a `RequestMessage` observed on-chain, against the same or a sibling wallet, reach `external` in `contracts/wallet/src/request/mod.rs` through the `no-sign` signature schema or the extension path so a `Request` executes without any signature check, breaking the invariant `every executed `NearAction` == one the owner signed, or one an owner-enabled extension requested` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/wallet/src/request/mod.rs](contracts/wallet/src/request/mod.rs) - `external` (cross-check `is_empty` in the same file)
- Entrypoint: replay of a `RequestMessage` observed on-chain, against the same or a sibling wallet
- Attacker controls: when it is replayed and which wallet instance it is sent to
- Exploit idea: `w_execute_extension` trusts `env::predecessor_account_id()` against the enabled-extension set; probe extension enable/disable ordering and the zero-deposit requirement. Set-up: the request is replayed against a sibling subwallet deployment.
- Invariant to test: every executed `NearAction` == one the owner signed, or one an owner-enabled extension requested
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Call `w_execute_extension` from a non-enabled predecessor and with zero deposit; assert rejection.
