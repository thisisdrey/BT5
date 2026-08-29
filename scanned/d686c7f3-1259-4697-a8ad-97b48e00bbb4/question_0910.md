# Q0910: send-tokens via supply-collateral-add: convert a rounding direction into a repeatable extraction

## Question
Entering through `supply-collateral-add` (mainnet/contracts/market/v0-4-market.clar:1175) while controlling the position state the final collateral-add is validated against, can an unprivileged attacker make `send-tokens` (mainnet/contracts/market/v0-market-vault.clar:259) convert a rounding direction into a repeatable extraction? `send-tokens` pushes an asset to a caller-chosen recipient principal, so the invariant that every asset a position holds enters the health evaluation exactly once would fail, yielding direct theft of user funds at rest or in motion.

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:259` -> `send-tokens`
- Entrypoint: `supply-collateral-add` (`mainnet/contracts/market/v0-4-market.clar:1175`), unprivileged and publicly callable
- Attacker controls: the position state the final collateral-add is validated against
- Exploit idea: `send-tokens` pushes an asset to a caller-chosen recipient principal. Reach it through `supply-collateral-add` and convert a rounding direction into a repeatable extraction.
- Invariant to test: every asset a position holds enters the health evaluation exactly once
- Expected Immunefi impact: Critical - direct theft of user funds at rest or in motion
- Fast validation: Set up the position in simnet, call `supply-collateral-add` with the position state the final collateral-add is validated against, and assert on the printed event plus the post-state that collateral, debt and share totals still reconcile.
