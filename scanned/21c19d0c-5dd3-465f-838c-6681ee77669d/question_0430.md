# Q0430: socialize-debt-asset via liquidate-multi: convert a rounding direction into a repeatable extraction

## Question
Entering through `liquidate-multi` (mainnet/contracts/market/v0-4-market.clar:1593) while controlling how many entries share one price snapshot (price-feeds is passed as none), can an unprivileged attacker make `socialize-debt-asset` (mainnet/contracts/market/v0-4-market.clar:879) convert a rounding direction into a repeatable extraction? `socialize-debt-asset` calls the vault write-down, then overwrites `index-cache` for the current timestamp mid-fold, and carries a `success` flag that short-circuits, so the invariant that every asset a position holds enters the health evaluation exactly once would fail, yielding theft of unclaimed yield.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:879` -> `socialize-debt-asset`
- Entrypoint: `liquidate-multi` (`mainnet/contracts/market/v0-4-market.clar:1593`), unprivileged and publicly callable
- Attacker controls: how many entries share one price snapshot (price-feeds is passed as none)
- Exploit idea: `socialize-debt-asset` calls the vault write-down, then overwrites `index-cache` for the current timestamp mid-fold, and carries a `success` flag that short-circuits. Reach it through `liquidate-multi` and convert a rounding direction into a repeatable extraction.
- Invariant to test: every asset a position holds enters the health evaluation exactly once
- Expected Immunefi impact: High - theft of unclaimed yield
- Fast validation: Set up the position in simnet, call `liquidate-multi` with how many entries share one price snapshot (price-feeds is passed as none), and assert on the printed event plus the post-state that collateral, debt and share totals still reconcile.
