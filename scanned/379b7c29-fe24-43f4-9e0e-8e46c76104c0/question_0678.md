# Q0678: accrue-debt-asset via collateral-remove-redeem: convert a rounding direction into a repeatable extraction

## Question
Entering through `collateral-remove-redeem` (mainnet/contracts/market/v0-4-market.clar:1211) while controlling `receiver` for the underlying leg, can an unprivileged attacker make `accrue-debt-asset` (mainnet/contracts/market/v0-4-market.clar:262) convert a rounding direction into a repeatable extraction? `accrue-debt-asset` calls `accrue-and-cache` with `unwrap-panic` inside a fold whose accumulator ignores the result, so the invariant that every asset a position holds enters the health evaluation exactly once would fail, yielding permanent freezing of funds.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:262` -> `accrue-debt-asset`
- Entrypoint: `collateral-remove-redeem` (`mainnet/contracts/market/v0-4-market.clar:1211`), unprivileged and publicly callable
- Attacker controls: `receiver` for the underlying leg
- Exploit idea: `accrue-debt-asset` calls `accrue-and-cache` with `unwrap-panic` inside a fold whose accumulator ignores the result. Reach it through `collateral-remove-redeem` and convert a rounding direction into a repeatable extraction.
- Invariant to test: every asset a position holds enters the health evaluation exactly once
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Fuzz `receiver` for the underlying leg across its boundary values through `collateral-remove-redeem` in simnet and assert `accrue-debt-asset` never returns a value that breaks the invariant.
