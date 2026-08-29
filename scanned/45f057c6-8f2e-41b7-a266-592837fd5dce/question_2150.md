# Q2150: resolve-or-create via liquidate: compose two individually correct mechanisms into an incorr

## Question
Entering through `liquidate` (mainnet/contracts/market/v0-4-market.clar:1382) while controlling `collateral-receiver`, can an unprivileged attacker make `resolve-or-create` (mainnet/contracts/market/v0-market-vault.clar:143) compose two individually correct mechanisms into an incorrect result? `resolve-or-create` allocates a user id through `increment` for whatever principal the market names, so the invariant that a vault's underlying plus outstanding debt covers all shares and all supplier claims would fail, yielding permanent freezing of unclaimed yield.

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:143` -> `resolve-or-create`
- Entrypoint: `liquidate` (`mainnet/contracts/market/v0-4-market.clar:1382`), unprivileged and publicly callable
- Attacker controls: `collateral-receiver`
- Exploit idea: `resolve-or-create` allocates a user id through `increment` for whatever principal the market names. Reach it through `liquidate` and compose two individually correct mechanisms into an incorrect result.
- Invariant to test: a vault's underlying plus outstanding debt covers all shares and all supplier claims
- Expected Immunefi impact: High - permanent freezing of unclaimed yield
- Fast validation: Write a Clarinet simnet test calling `liquidate` twice with `collateral-receiver` varied, and assert that the value `resolve-or-create` returns is identical in both runs; a divergence confirms the finding.
