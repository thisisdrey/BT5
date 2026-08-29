# Q0294: find-asset via liquidate: convert a rounding direction into a repeatable extraction

## Question
Entering through `liquidate` (mainnet/contracts/market/v0-4-market.clar:1382) while controlling the `price-feeds` buffers and their ordering, can an unprivileged attacker make `find-asset` (mainnet/contracts/market/v0-4-market.clar:584) convert a rounding direction into a repeatable extraction? `find-asset` returns `none` when the id is absent, and several callers resolve that with `unwrap-panic`, so the invariant that every asset a position holds enters the health evaluation exactly once would fail, yielding permanent freezing of a position that can never be closed.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:584` -> `find-asset`
- Entrypoint: `liquidate` (`mainnet/contracts/market/v0-4-market.clar:1382`), unprivileged and publicly callable
- Attacker controls: the `price-feeds` buffers and their ordering
- Exploit idea: `find-asset` returns `none` when the id is absent, and several callers resolve that with `unwrap-panic`. Reach it through `liquidate` and convert a rounding direction into a repeatable extraction.
- Invariant to test: every asset a position holds enters the health evaluation exactly once
- Expected Immunefi impact: Critical - permanent freezing of a position that can never be closed
- Fast validation: Fuzz the `price-feeds` buffers and their ordering across its boundary values through `liquidate` in simnet and assert `find-asset` never returns a value that breaks the invariant.
