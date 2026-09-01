# Q2190: lib - no-sign or extension path reachable without authorisation (16)

## Question
Given the wallet's nonce bitmap has just rotated `current` into `old`, can an unprivileged attacker, entering through an on-chain call that triggers nonce rotation or cleanup before a victim's request lands, reach `WalletWebauthnProof` in `contracts/wallet/signatures/webauthn/src/lib.rs` through the `no-sign` signature schema or the extension path so a `Request` executes without any signature check, breaking the invariant `every executed `NearAction` == one the owner signed, or one an owner-enabled extension requested` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/wallet/signatures/webauthn/src/lib.rs](contracts/wallet/signatures/webauthn/src/lib.rs) - `WalletWebauthnProof` (cross-check `verify_request_msg` in the same file)
- Entrypoint: an on-chain call that triggers nonce rotation or cleanup before a victim's request lands
- Attacker controls: the timing of the triggering call
- Exploit idea: `w_execute_extension` trusts `env::predecessor_account_id()` against the enabled-extension set; probe extension enable/disable ordering and the zero-deposit requirement. Set-up: the wallet's nonce bitmap has just rotated `current` into `old`.
- Invariant to test: every executed `NearAction` == one the owner signed, or one an owner-enabled extension requested
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Call `w_execute_extension` from a non-enabled predecessor and with zero deposit; assert rejection.
