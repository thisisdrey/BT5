# Q5660: receive-tokens via supply-collateral-add: make a health check read a different position than the one

## Question
Does `supply-collateral-add` (mainnet/contracts/market/v0-4-market.clar:1175) let an unprivileged attacker who controls the position state the final collateral-add is validated against reach `receive-tokens` (mainnet/contracts/market/v0-market-vault.clar:256) in a state where it make a health check read a different position than the one that will exist? Given that it pulls an asset from a named account, the invariant that a value cached within a block still describes the state it was derived from breaks and the result is permanent freezing of unclaimed yield.

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:256` -> `receive-tokens`
- Entrypoint: `supply-collateral-add` (`mainnet/contracts/market/v0-4-market.clar:1175`), unprivileged and publicly callable
- Attacker controls: the position state the final collateral-add is validated against
- Exploit idea: `receive-tokens` pulls an asset from a named account. Reach it through `supply-collateral-add` and make a health check read a different position than the one that will exist.
- Invariant to test: a value cached within a block still describes the state it was derived from
- Expected Immunefi impact: High - permanent freezing of unclaimed yield
- Fast validation: Write a Clarinet simnet test calling `supply-collateral-add` twice with the position state the final collateral-add is validated against varied, and assert that the value `receive-tokens` returns is identical in both runs; a divergence confirms the finding.
