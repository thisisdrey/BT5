# Q4868: process-collateral-asset via liquidate: convert a rounding direction into a repeatable extraction

## Question
Does `liquidate` (mainnet/contracts/market/v0-4-market.clar:1382) let an unprivileged attacker who controls the `price-feeds` buffers and their ordering reach `process-collateral-asset` (mainnet/contracts/market/v0-4-market.clar:789) in a state where it convert a rounding direction into a repeatable extraction? Given that it computes expected collateral, then caps it at the borrower's balance, the invariant that no position row exists that the position mask does not represent breaks and the result is permanent freezing of unclaimed yield.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:789` -> `process-collateral-asset`
- Entrypoint: `liquidate` (`mainnet/contracts/market/v0-4-market.clar:1382`), unprivileged and publicly callable
- Attacker controls: the `price-feeds` buffers and their ordering
- Exploit idea: `process-collateral-asset` computes expected collateral, then caps it at the borrower's balance. Reach it through `liquidate` and convert a rounding direction into a repeatable extraction.
- Invariant to test: no position row exists that the position mask does not represent
- Expected Immunefi impact: High - permanent freezing of unclaimed yield
- Fast validation: Write a Clarinet simnet test calling `liquidate` twice with the `price-feeds` buffers and their ordering varied, and assert that the value `process-collateral-asset` returns is identical in both runs; a divergence confirms the finding.
