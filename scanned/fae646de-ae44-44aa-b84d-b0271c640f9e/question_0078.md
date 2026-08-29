# Q0078: find-superset via collateral-remove: convert a rounding direction into a repeatable extraction

## Question
Entering through `collateral-remove` (mainnet/contracts/market/v0-4-market.clar:1107) while controlling the set of assets held, can an unprivileged attacker make `find-superset` (mainnet/contracts/registry/v0-egroup.clar:262) convert a rounding direction into a repeatable extraction? `find-superset` returns the FIRST mask that is a superset, walking buckets in population order rather than finding the tightest, so the invariant that every asset a position holds enters the health evaluation exactly once would fail, yielding permanent freezing of a position that can never be closed.

## Target
- File/function: `mainnet/contracts/registry/v0-egroup.clar:262` -> `find-superset`
- Entrypoint: `collateral-remove` (`mainnet/contracts/market/v0-4-market.clar:1107`), unprivileged and publicly callable
- Attacker controls: the set of assets held
- Exploit idea: `find-superset` returns the FIRST mask that is a superset, walking buckets in population order rather than finding the tightest. Reach it through `collateral-remove` and convert a rounding direction into a repeatable extraction.
- Invariant to test: every asset a position holds enters the health evaluation exactly once
- Expected Immunefi impact: Critical - permanent freezing of a position that can never be closed
- Fast validation: Fuzz the set of assets held across its boundary values through `collateral-remove` in simnet and assert `find-superset` never returns a value that breaks the invariant.
