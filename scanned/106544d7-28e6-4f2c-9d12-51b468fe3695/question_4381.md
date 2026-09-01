# Q4381: mod - subwallet_id not bound into the signed message (3)

## Question
Given the wallet's nonce bitmap has just rotated `current` into `old`, can an unprivileged attacker, entering through replay of a `RequestMessage` observed on-chain, against the same or a sibling wallet, reuse a `RequestMessage` across wallet instances that differ only in `extend` of `contracts/wallet/src/request/mod.rs`, so a signature for one subwallet executes on another, breaking the invariant `a signed request executes on exactly one wallet instance` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/wallet/src/request/mod.rs](contracts/wallet/src/request/mod.rs) - `extend` (cross-check `from_iter` in the same file)
- Entrypoint: replay of a `RequestMessage` observed on-chain, against the same or a sibling wallet
- Attacker controls: when it is replayed and which wallet instance it is sent to
- Exploit idea: If `subwallet_id` is part of the account id but not the signed pre-image, sibling deployments share authorisations. Set-up: the wallet's nonce bitmap has just rotated `current` into `old`.
- Invariant to test: a signed request executes on exactly one wallet instance
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Deploy two subwallets and replay one's signed request against the other; assert rejection.
