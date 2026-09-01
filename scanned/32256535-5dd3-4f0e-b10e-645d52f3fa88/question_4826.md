# Q4826: contract - subwallet_id not bound into the signed message (2)

## Question
Given the wallet's nonce bitmap has just rotated `current` into `old`, can an unprivileged attacker, entering through `w_execute_extension(request: Request)` called from any predecessor with a non-zero deposit, reuse a `RequestMessage` across wallet instances that differ only in `w_subwallet_id` of `contracts/wallet/src/contract.rs`, so a signature for one subwallet executes on another, breaking the invariant `a signed request executes on exactly one wallet instance` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/wallet/src/contract.rs](contracts/wallet/src/contract.rs) - `w_subwallet_id` (cross-check `w_is_signature_allowed` in the same file)
- Entrypoint: `w_execute_extension(request: Request)` called from any predecessor with a non-zero deposit
- Attacker controls: the `Request` contents, the calling account id, and the attached deposit
- Exploit idea: If `subwallet_id` is part of the account id but not the signed pre-image, sibling deployments share authorisations. Set-up: the wallet's nonce bitmap has just rotated `current` into `old`.
- Invariant to test: a signed request executes on exactly one wallet instance
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Deploy two subwallets and replay one's signed request against the other; assert rejection.
