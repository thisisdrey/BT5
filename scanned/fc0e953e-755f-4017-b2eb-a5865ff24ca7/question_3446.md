# Q3446: lib - AuthResolver resolution accepted for the wrong signer (6)

## Question
Given the request is replayed against a sibling subwallet deployment, can an unprivileged attacker, entering through `w_execute_extension(request: Request)` called from any predecessor with a non-zero deposit, make `PendingAuthorization` in `crates/signatures/nep641/src/lib.rs` return an `AuthorizationResolution` that authorises a `signer_id` other than the one the resolved access key or contract actually belongs to, breaking the invariant `the `signer_id` an `AuthorizationResolution` authorises == the account whose key was actually resolved` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [crates/signatures/nep641/src/lib.rs](crates/signatures/nep641/src/lib.rs) - `PendingAuthorization` (cross-check `w_resolve_auth` in the same file)
- Entrypoint: `w_execute_extension(request: Request)` called from any predecessor with a non-zero deposit
- Attacker controls: the `Request` contents, the calling account id, and the attached deposit
- Exploit idea: NEP-641 resolution maps an account/key to an authorisation decision; probe caching, stale key sets, and default-allow branches. Set-up: the request is replayed against a sibling subwallet deployment.
- Invariant to test: the `signer_id` an `AuthorizationResolution` authorises == the account whose key was actually resolved
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Resolve against a rotated/removed key; assert authorisation is refused.
