# Q4338: active via liquidate: satisfy a bound with a value the bound was never designed 

## Question
Entering through `liquidate` (mainnet/contracts/market/v0-4-market.clar:1382) while controlling `collateral-receiver`, can an unprivileged attacker make `active` (mainnet/contracts/registry/v0-egroup.clar:238) satisfy a bound with a value the bound was never designed to admit? `active` lists candidate bucket masks at or above a population, so the invariant that every asset a position holds enters the health evaluation exactly once would fail, yielding temporary freezing of funds.

## Target
- File/function: `mainnet/contracts/registry/v0-egroup.clar:238` -> `active`
- Entrypoint: `liquidate` (`mainnet/contracts/market/v0-4-market.clar:1382`), unprivileged and publicly callable
- Attacker controls: `collateral-receiver`
- Exploit idea: `active` lists candidate bucket masks at or above a population. Reach it through `liquidate` and satisfy a bound with a value the bound was never designed to admit.
- Invariant to test: every asset a position holds enters the health evaluation exactly once
- Expected Immunefi impact: High - temporary freezing of funds
- Fast validation: Fuzz `collateral-receiver` across its boundary values through `liquidate` in simnet and assert `active` never returns a value that breaks the invariant.
