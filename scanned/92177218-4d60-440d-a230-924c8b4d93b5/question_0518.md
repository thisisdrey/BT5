# Q0518: active via collateral-add: convert a rounding direction into a repeatable extraction

## Question
Entering through `collateral-add` (mainnet/contracts/market/v0-4-market.clar:1020) while controlling `amount`, can an unprivileged attacker make `active` (mainnet/contracts/registry/v0-egroup.clar:238) convert a rounding direction into a repeatable extraction? `active` lists candidate bucket masks at or above a population, so the invariant that every asset a position holds enters the health evaluation exactly once would fail, yielding protocol insolvency through uncollateralised debt.

## Target
- File/function: `mainnet/contracts/registry/v0-egroup.clar:238` -> `active`
- Entrypoint: `collateral-add` (`mainnet/contracts/market/v0-4-market.clar:1020`), unprivileged and publicly callable
- Attacker controls: `amount`
- Exploit idea: `active` lists candidate bucket masks at or above a population. Reach it through `collateral-add` and convert a rounding direction into a repeatable extraction.
- Invariant to test: every asset a position holds enters the health evaluation exactly once
- Expected Immunefi impact: Critical - protocol insolvency through uncollateralised debt
- Fast validation: Write a Clarinet simnet test calling `collateral-add` twice with `amount` varied, and assert that the value `active` returns is identical in both runs; a divergence confirms the finding.
