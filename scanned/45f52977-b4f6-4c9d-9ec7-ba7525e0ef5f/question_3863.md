# Q3863: interest-rate via call-ststx-ratio: make an aggregate and its per-item breakdown disagree

## Question
`interest-rate` (mainnet/contracts/vault/v0-vault-stx.clar:371) interpolates the packed curve at the current utilization. Can an unprivileged caller of `call-ststx-ratio` (mainnet/contracts/market/v0-4-market.clar:1015), by choosing whether the ratio is fetched before or after other state changes in the block, use that to make an aggregate and its per-item breakdown disagree, violating the invariant that no position row exists that the position mask does not represent and producing protocol insolvency?

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:371` -> `interest-rate`
- Entrypoint: `call-ststx-ratio` (`mainnet/contracts/market/v0-4-market.clar:1015`), unprivileged and publicly callable
- Attacker controls: whether the ratio is fetched before or after other state changes in the block
- Exploit idea: `interest-rate` interpolates the packed curve at the current utilization. Reach it through `call-ststx-ratio` and make an aggregate and its per-item breakdown disagree.
- Invariant to test: no position row exists that the position mask does not represent
- Expected Immunefi impact: Critical - protocol insolvency
- Fast validation: Run the baseline `call-ststx-ratio` call, then the attacker-shaped one with whether the ratio is fetched before or after other state changes in the block, and assert the attacker's net token balance change is zero or negative.
