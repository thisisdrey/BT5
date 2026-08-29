# Q0622: active via collateral-remove: convert a rounding direction into a repeatable extraction

## Question
Entering through `collateral-remove` (mainnet/contracts/market/v0-4-market.clar:1107) while controlling whether the position has any enabled debt row (the has-debt branch), can an unprivileged attacker make `active` (mainnet/contracts/registry/v0-egroup.clar:238) convert a rounding direction into a repeatable extraction? `active` lists candidate bucket masks at or above a population, so the invariant that every asset a position holds enters the health evaluation exactly once would fail, yielding direct theft of user funds at rest or in motion.

## Target
- File/function: `mainnet/contracts/registry/v0-egroup.clar:238` -> `active`
- Entrypoint: `collateral-remove` (`mainnet/contracts/market/v0-4-market.clar:1107`), unprivileged and publicly callable
- Attacker controls: whether the position has any enabled debt row (the has-debt branch)
- Exploit idea: `active` lists candidate bucket masks at or above a population. Reach it through `collateral-remove` and convert a rounding direction into a repeatable extraction.
- Invariant to test: every asset a position holds enters the health evaluation exactly once
- Expected Immunefi impact: Critical - direct theft of user funds at rest or in motion
- Fast validation: Set up the position in simnet, call `collateral-remove` with whether the position has any enabled debt row (the has-debt branch), and assert on the printed event plus the post-state that collateral, debt and share totals still reconcile.
