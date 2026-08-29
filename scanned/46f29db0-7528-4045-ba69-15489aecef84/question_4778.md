# Q4778: refresh via liquidate: satisfy a bound with a value the bound was never designed 

## Question
Entering through `liquidate` (mainnet/contracts/market/v0-4-market.clar:1382) while controlling the `price-feeds` buffers and their ordering, can an unprivileged attacker make `refresh` (mainnet/contracts/market/v0-market-vault.clar:171) satisfy a bound with a value the bound was never designed to admit? `refresh` rewrites `mask` and stamps `last-update` to `stacks-block-time` on every write, so the invariant that every asset a position holds enters the health evaluation exactly once would fail, yielding permanent freezing of unclaimed yield.

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:171` -> `refresh`
- Entrypoint: `liquidate` (`mainnet/contracts/market/v0-4-market.clar:1382`), unprivileged and publicly callable
- Attacker controls: the `price-feeds` buffers and their ordering
- Exploit idea: `refresh` rewrites `mask` and stamps `last-update` to `stacks-block-time` on every write. Reach it through `liquidate` and satisfy a bound with a value the bound was never designed to admit.
- Invariant to test: every asset a position holds enters the health evaluation exactly once
- Expected Immunefi impact: High - permanent freezing of unclaimed yield
- Fast validation: Write a Clarinet simnet test calling `liquidate` twice with the `price-feeds` buffers and their ordering varied, and assert that the value `refresh` returns is identical in both runs; a divergence confirms the finding.
