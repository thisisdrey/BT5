# Q3909: lib - subwallet_id not bound into the signed message (5)

## Question
Given the wallet's nonce bitmap has just rotated `current` into `old`, can an unprivileged attacker, entering through `w_execute_extension(request: Request)` called from any predecessor with a non-zero deposit, reuse a `RequestMessage` across wallet instances that differ only in `NoSign` of `contracts/wallet/signatures/no-sign/src/lib.rs`, so a signature for one subwallet executes on another, breaking the invariant `a signed request executes on exactly one wallet instance` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/wallet/signatures/no-sign/src/lib.rs](contracts/wallet/signatures/no-sign/src/lib.rs) - `NoSign` (cross-check `verify_offchain_msg` in the same file)
- Entrypoint: `w_execute_extension(request: Request)` called from any predecessor with a non-zero deposit
- Attacker controls: the `Request` contents, the calling account id, and the attached deposit
- Exploit idea: If `subwallet_id` is part of the account id but not the signed pre-image, sibling deployments share authorisations. Set-up: the wallet's nonce bitmap has just rotated `current` into `old`.
- Invariant to test: a signed request executes on exactly one wallet instance
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Deploy two subwallets and replay one's signed request against the other; assert rejection.
