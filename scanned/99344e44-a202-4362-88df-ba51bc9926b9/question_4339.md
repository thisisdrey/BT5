# Q4339: price-resolve via collateral-add: make two code sites that must agree disagree by an attacke

## Question
`price-resolve` (mainnet/contracts/market/v0-4-market.clar:373) resolves a feed, applies the callcode transform, then checks `oracle-price-legal` and `oracle-timestamp-fresh` on the POST-transform value while advancing the per-key `last-update` only when the new timestamp is greater. Can an unprivileged caller of `collateral-add` (mainnet/contracts/market/v0-4-market.clar:1020), by choosing whether this asset is already collateral (the is-new-collateral branch), use that to make two code sites that must agree disagree by an attacker-chosen amount, violating the invariant that conversions never round in the user's favour in either direction and producing direct theft of user funds at rest or in motion?

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:373` -> `price-resolve`
- Entrypoint: `collateral-add` (`mainnet/contracts/market/v0-4-market.clar:1020`), unprivileged and publicly callable
- Attacker controls: whether this asset is already collateral (the is-new-collateral branch)
- Exploit idea: `price-resolve` resolves a feed, applies the callcode transform, then checks `oracle-price-legal` and `oracle-timestamp-fresh` on the POST-transform value while advancing the per-key `last-update` only when the new timestamp is greater. Reach it through `collateral-add` and make two code sites that must agree disagree by an attacker-chosen amount.
- Invariant to test: conversions never round in the user's favour in either direction
- Expected Immunefi impact: Critical - direct theft of user funds at rest or in motion
- Fast validation: In `local-testing/tests` on a local fork, drive `collateral-add` with whether this asset is already collateral (the is-new-collateral branch), then read `price-resolve` state before and after in the same block and assert the two sides of the invariant are equal.
