# Q1796: collateral-remove via transfer: make an aggregate and its per-item breakdown disagree

## Question
Does `transfer` (mainnet/contracts/vault/v0-vault-stx.clar:752) let an unprivileged attacker who controls the timing relative to a pledge or a liquidation reach `collateral-remove` (mainnet/contracts/market/v0-market-vault.clar:406) in a state where it make an aggregate and its per-item breakdown disagree? Given that it decrements the map and writes the entry before `send-tokens` executes, the invariant that conversions never round in the user's favour in either direction breaks and the result is protocol insolvency through uncollateralised debt.

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:406` -> `collateral-remove`
- Entrypoint: `transfer` (`mainnet/contracts/vault/v0-vault-stx.clar:752`), unprivileged and publicly callable
- Attacker controls: the timing relative to a pledge or a liquidation
- Exploit idea: `collateral-remove` decrements the map and writes the entry before `send-tokens` executes. Reach it through `transfer` and make an aggregate and its per-item breakdown disagree.
- Invariant to test: conversions never round in the user's favour in either direction
- Expected Immunefi impact: Critical - protocol insolvency through uncollateralised debt
- Fast validation: Write a Clarinet simnet test calling `transfer` twice with the timing relative to a pledge or a liquidation varied, and assert that the value `collateral-remove` returns is identical in both runs; a divergence confirms the finding.
