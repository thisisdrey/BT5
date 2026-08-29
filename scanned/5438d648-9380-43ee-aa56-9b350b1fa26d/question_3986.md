# Q3986: unwrap-status via collateral-add: satisfy a bound with a value the bound was never designed 

## Question
Entering through `collateral-add` (mainnet/contracts/market/v0-4-market.clar:1020) while controlling call ordering within the block, can an unprivileged attacker make `unwrap-status` (mainnet/contracts/registry/v0-assets.clar:111) satisfy a bound with a value the bound was never designed to admit? `unwrap-status` resolves `status` with `unwrap-panic`, so the invariant that every asset a position holds enters the health evaluation exactly once would fail, yielding protocol insolvency.

## Target
- File/function: `mainnet/contracts/registry/v0-assets.clar:111` -> `unwrap-status`
- Entrypoint: `collateral-add` (`mainnet/contracts/market/v0-4-market.clar:1020`), unprivileged and publicly callable
- Attacker controls: call ordering within the block
- Exploit idea: `unwrap-status` resolves `status` with `unwrap-panic`. Reach it through `collateral-add` and satisfy a bound with a value the bound was never designed to admit.
- Invariant to test: every asset a position holds enters the health evaluation exactly once
- Expected Immunefi impact: Critical - protocol insolvency
- Fast validation: Write a Clarinet simnet test calling `collateral-add` twice with call ordering within the block varied, and assert that the value `unwrap-status` returns is identical in both runs; a divergence confirms the finding.
