# Q5828: mask-to-list-collateral via supply-collateral-add: make a health check read a different position than the one

## Question
Does `supply-collateral-add` (mainnet/contracts/market/v0-4-market.clar:1175) let an unprivileged attacker who controls the `ft` trait principal deciding which vault is routed to reach `mask-to-list-collateral` (mainnet/contracts/market/v0-4-market.clar:449) in a state where it make a health check read a different position than the one that will exist? Given that it expands a mask to a list of ids over ITER-UINT-64, the invariant that a value cached within a block still describes the state it was derived from breaks and the result is protocol insolvency through uncollateralised debt.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:449` -> `mask-to-list-collateral`
- Entrypoint: `supply-collateral-add` (`mainnet/contracts/market/v0-4-market.clar:1175`), unprivileged and publicly callable
- Attacker controls: the `ft` trait principal deciding which vault is routed to
- Exploit idea: `mask-to-list-collateral` expands a mask to a list of ids over ITER-UINT-64. Reach it through `supply-collateral-add` and make a health check read a different position than the one that will exist.
- Invariant to test: a value cached within a block still describes the state it was derived from
- Expected Immunefi impact: Critical - protocol insolvency through uncollateralised debt
- Fast validation: Write a Clarinet simnet test calling `supply-collateral-add` twice with the `ft` trait principal deciding which vault is routed to varied, and assert that the value `mask-to-list-collateral` returns is identical in both runs; a divergence confirms the finding.
