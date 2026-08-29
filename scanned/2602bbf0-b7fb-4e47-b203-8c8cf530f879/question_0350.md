# Q0350: get-asset-value via collateral-remove: convert a rounding direction into a repeatable extraction

## Question
Entering through `collateral-remove` (mainnet/contracts/market/v0-4-market.clar:1107) while controlling `receiver`, including a contract principal, can an unprivileged attacker make `get-asset-value` (mainnet/contracts/market/v0-4-market.clar:679) convert a rounding direction into a repeatable extraction? `get-asset-value` resolves a fresh price for a single asset and normalizes with a caller-supplied rounding direction, so the invariant that every asset a position holds enters the health evaluation exactly once would fail, yielding permanent freezing of unclaimed yield.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:679` -> `get-asset-value`
- Entrypoint: `collateral-remove` (`mainnet/contracts/market/v0-4-market.clar:1107`), unprivileged and publicly callable
- Attacker controls: `receiver`, including a contract principal
- Exploit idea: `get-asset-value` resolves a fresh price for a single asset and normalizes with a caller-supplied rounding direction. Reach it through `collateral-remove` and convert a rounding direction into a repeatable extraction.
- Invariant to test: every asset a position holds enters the health evaluation exactly once
- Expected Immunefi impact: High - permanent freezing of unclaimed yield
- Fast validation: Write a Clarinet simnet test calling `collateral-remove` twice with `receiver`, including a contract principal varied, and assert that the value `get-asset-value` returns is identical in both runs; a divergence confirms the finding.
