# Q3455: active via collateral-add: make an aggregate and its per-item breakdown disagree

## Question
`active` (mainnet/contracts/registry/v0-egroup.clar:238) lists candidate bucket masks at or above a population. Can an unprivileged caller of `collateral-add` (mainnet/contracts/market/v0-4-market.clar:1020), by choosing `amount`, use that to make an aggregate and its per-item breakdown disagree, violating the invariant that no position row exists that the position mask does not represent and producing protocol insolvency through uncollateralised debt?

## Target
- File/function: `mainnet/contracts/registry/v0-egroup.clar:238` -> `active`
- Entrypoint: `collateral-add` (`mainnet/contracts/market/v0-4-market.clar:1020`), unprivileged and publicly callable
- Attacker controls: `amount`
- Exploit idea: `active` lists candidate bucket masks at or above a population. Reach it through `collateral-add` and make an aggregate and its per-item breakdown disagree.
- Invariant to test: no position row exists that the position mask does not represent
- Expected Immunefi impact: Critical - protocol insolvency through uncollateralised debt
- Fast validation: Run the baseline `collateral-add` call, then the attacker-shaped one with `amount`, and assert the attacker's net token balance change is zero or negative.
