# Q5936: filter-out-debt-asset via collateral-remove-redeem: compose two individually correct mechanisms into an incorr

## Question
Does `collateral-remove-redeem` (mainnet/contracts/market/v0-4-market.clar:1211) let an unprivileged attacker who controls `min-underlying` reach `filter-out-debt-asset` (mainnet/contracts/market/v0-4-market.clar:633) in a state where it compose two individually correct mechanisms into an incorrect result? Given that it rebuilds the debt list without one asset, under `as-max-len? ... u64`, the invariant that a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction breaks and the result is protocol insolvency through uncollateralised debt.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:633` -> `filter-out-debt-asset`
- Entrypoint: `collateral-remove-redeem` (`mainnet/contracts/market/v0-4-market.clar:1211`), unprivileged and publicly callable
- Attacker controls: `min-underlying`
- Exploit idea: `filter-out-debt-asset` rebuilds the debt list without one asset, under `as-max-len? ... u64`. Reach it through `collateral-remove-redeem` and compose two individually correct mechanisms into an incorrect result.
- Invariant to test: a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction
- Expected Immunefi impact: Critical - protocol insolvency through uncollateralised debt
- Fast validation: Write a Clarinet simnet test calling `collateral-remove-redeem` twice with `min-underlying` varied, and assert that the value `filter-out-debt-asset` returns is identical in both runs; a divergence confirms the finding.
