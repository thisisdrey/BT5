# Q0296: mask-to-list-collateral via collateral-add: satisfy a bound with a value the bound was never designed 

## Question
Does `collateral-add` (mainnet/contracts/market/v0-4-market.clar:1020) let an unprivileged attacker who controls `amount` reach `mask-to-list-collateral` (mainnet/contracts/market/v0-4-market.clar:449) in a state where it satisfy a bound with a value the bound was never designed to admit? Given that it expands a mask to a list of ids over ITER-UINT-64, the invariant that no position row exists that the position mask does not represent breaks and the result is protocol insolvency.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:449` -> `mask-to-list-collateral`
- Entrypoint: `collateral-add` (`mainnet/contracts/market/v0-4-market.clar:1020`), unprivileged and publicly callable
- Attacker controls: `amount`
- Exploit idea: `mask-to-list-collateral` expands a mask to a list of ids over ITER-UINT-64. Reach it through `collateral-add` and satisfy a bound with a value the bound was never designed to admit.
- Invariant to test: no position row exists that the position mask does not represent
- Expected Immunefi impact: Critical - protocol insolvency
- Fast validation: Write a Clarinet simnet test calling `collateral-add` twice with `amount` varied, and assert that the value `mask-to-list-collateral` returns is identical in both runs; a divergence confirms the finding.
