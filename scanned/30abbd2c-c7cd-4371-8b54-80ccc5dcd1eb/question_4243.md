# Q4243: uint-to-list-u64 via collateral-add: make two code sites that must agree disagree by an attacke

## Question
`uint-to-list-u64` (mainnet/contracts/registry/v0-assets.clar:80) expands a bitmap into a 64-element list. Can an unprivileged caller of `collateral-add` (mainnet/contracts/market/v0-4-market.clar:1020), by choosing whether this asset is already collateral (the is-new-collateral branch), use that to make two code sites that must agree disagree by an attacker-chosen amount, violating the invariant that conversions never round in the user's favour in either direction and producing direct theft of another user's collateral?

## Target
- File/function: `mainnet/contracts/registry/v0-assets.clar:80` -> `uint-to-list-u64`
- Entrypoint: `collateral-add` (`mainnet/contracts/market/v0-4-market.clar:1020`), unprivileged and publicly callable
- Attacker controls: whether this asset is already collateral (the is-new-collateral branch)
- Exploit idea: `uint-to-list-u64` expands a bitmap into a 64-element list. Reach it through `collateral-add` and make two code sites that must agree disagree by an attacker-chosen amount.
- Invariant to test: conversions never round in the user's favour in either direction
- Expected Immunefi impact: Critical - direct theft of another user's collateral
- Fast validation: In `local-testing/tests` on a local fork, drive `collateral-add` with whether this asset is already collateral (the is-new-collateral branch), then read `uint-to-list-u64` state before and after in the same block and assert the two sides of the invariant are equal.
