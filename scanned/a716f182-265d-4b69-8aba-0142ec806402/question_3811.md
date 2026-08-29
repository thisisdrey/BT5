# Q3811: send-underlying via collateral-remove-redeem: make an aggregate and its per-item breakdown disagree

## Question
`send-underlying` (mainnet/contracts/vault/v0-vault-stx.clar:296) pushes the underlying under an `as-contract?` post-condition scope. Can an unprivileged caller of `collateral-remove-redeem` (mainnet/contracts/market/v0-4-market.clar:1211), by choosing the zToken/underlying id mapping reached (the u100 sentinel branch), use that to make an aggregate and its per-item breakdown disagree, violating the invariant that a vault's underlying plus outstanding debt covers all shares and all supplier claims and producing direct theft of another user's collateral?

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:296` -> `send-underlying`
- Entrypoint: `collateral-remove-redeem` (`mainnet/contracts/market/v0-4-market.clar:1211`), unprivileged and publicly callable
- Attacker controls: the zToken/underlying id mapping reached (the u100 sentinel branch)
- Exploit idea: `send-underlying` pushes the underlying under an `as-contract?` post-condition scope. Reach it through `collateral-remove-redeem` and make an aggregate and its per-item breakdown disagree.
- Invariant to test: a vault's underlying plus outstanding debt covers all shares and all supplier claims
- Expected Immunefi impact: Critical - direct theft of another user's collateral
- Fast validation: In `local-testing/tests` on a local fork, drive `collateral-remove-redeem` with the zToken/underlying id mapping reached (the u100 sentinel branch), then read `send-underlying` state before and after in the same block and assert the two sides of the invariant are equal.
