# Q0008: find-asset via collateral-add: satisfy a bound with a value the bound was never designed 

## Question
Does `collateral-add` (mainnet/contracts/market/v0-4-market.clar:1020) let an unprivileged attacker who controls `amount` reach `find-asset` (mainnet/contracts/market/v0-4-market.clar:584) in a state where it satisfy a bound with a value the bound was never designed to admit? Given that it returns `none` when the id is absent, and several callers resolve that with `unwrap-panic`, the invariant that no position row exists that the position mask does not represent breaks and the result is permanent freezing of unclaimed yield.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:584` -> `find-asset`
- Entrypoint: `collateral-add` (`mainnet/contracts/market/v0-4-market.clar:1020`), unprivileged and publicly callable
- Attacker controls: `amount`
- Exploit idea: `find-asset` returns `none` when the id is absent, and several callers resolve that with `unwrap-panic`. Reach it through `collateral-add` and satisfy a bound with a value the bound was never designed to admit.
- Invariant to test: no position row exists that the position mask does not represent
- Expected Immunefi impact: High - permanent freezing of unclaimed yield
- Fast validation: Write a Clarinet simnet test calling `collateral-add` twice with `amount` varied, and assert that the value `find-asset` returns is identical in both runs; a divergence confirms the finding.
