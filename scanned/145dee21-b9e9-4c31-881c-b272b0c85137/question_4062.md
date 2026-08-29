# Q4062: unwrap-status via liquidate: satisfy a bound with a value the bound was never designed 

## Question
Entering through `liquidate` (mainnet/contracts/market/v0-4-market.clar:1382) while controlling `collateral-receiver`, can an unprivileged attacker make `unwrap-status` (mainnet/contracts/registry/v0-assets.clar:111) satisfy a bound with a value the bound was never designed to admit? `unwrap-status` resolves `status` with `unwrap-panic`, so the invariant that a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction would fail, yielding permanent freezing of funds.

## Target
- File/function: `mainnet/contracts/registry/v0-assets.clar:111` -> `unwrap-status`
- Entrypoint: `liquidate` (`mainnet/contracts/market/v0-4-market.clar:1382`), unprivileged and publicly callable
- Attacker controls: `collateral-receiver`
- Exploit idea: `unwrap-status` resolves `status` with `unwrap-panic`. Reach it through `liquidate` and satisfy a bound with a value the bound was never designed to admit.
- Invariant to test: a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Fuzz `collateral-receiver` across its boundary values through `liquidate` in simnet and assert `unwrap-status` never returns a value that breaks the invariant.
