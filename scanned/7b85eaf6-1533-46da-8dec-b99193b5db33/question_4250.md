# Q4250: add-user-collateral via liquidate: satisfy a bound with a value the bound was never designed 

## Question
Entering through `liquidate` (mainnet/contracts/market/v0-4-market.clar:1382) while controlling the `price-feeds` buffers and their ordering, can an unprivileged attacker make `add-user-collateral` (mainnet/contracts/market/v0-market-vault.clar:198) satisfy a bound with a value the bound was never designed to admit? `add-user-collateral` adds to the collateral row with a graceful u0 default, so the invariant that every asset a position holds enters the health evaluation exactly once would fail, yielding protocol insolvency.

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:198` -> `add-user-collateral`
- Entrypoint: `liquidate` (`mainnet/contracts/market/v0-4-market.clar:1382`), unprivileged and publicly callable
- Attacker controls: the `price-feeds` buffers and their ordering
- Exploit idea: `add-user-collateral` adds to the collateral row with a graceful u0 default. Reach it through `liquidate` and satisfy a bound with a value the bound was never designed to admit.
- Invariant to test: every asset a position holds enters the health evaluation exactly once
- Expected Immunefi impact: Critical - protocol insolvency
- Fast validation: Write a Clarinet simnet test calling `liquidate` twice with the `price-feeds` buffers and their ordering varied, and assert that the value `add-user-collateral` returns is identical in both runs; a divergence confirms the finding.
