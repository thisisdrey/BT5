# Q4196: price-multi-resolve via collateral-remove: convert a rounding direction into a repeatable extraction

## Question
Does `collateral-remove` (mainnet/contracts/market/v0-4-market.clar:1107) let an unprivileged attacker who controls the set of assets held reach `price-multi-resolve` (mainnet/contracts/market/v0-4-market.clar:397) in a state where it convert a rounding direction into a repeatable extraction? Given that it folds `iter-price-multi` into a POSITIONAL price list, asserting only the `valid` flag at the end, the invariant that no position row exists that the position mask does not represent breaks and the result is protocol insolvency.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:397` -> `price-multi-resolve`
- Entrypoint: `collateral-remove` (`mainnet/contracts/market/v0-4-market.clar:1107`), unprivileged and publicly callable
- Attacker controls: the set of assets held
- Exploit idea: `price-multi-resolve` folds `iter-price-multi` into a POSITIONAL price list, asserting only the `valid` flag at the end. Reach it through `collateral-remove` and convert a rounding direction into a repeatable extraction.
- Invariant to test: no position row exists that the position mask does not represent
- Expected Immunefi impact: Critical - protocol insolvency
- Fast validation: Write a Clarinet simnet test calling `collateral-remove` twice with the set of assets held varied, and assert that the value `price-multi-resolve` returns is identical in both runs; a divergence confirms the finding.
