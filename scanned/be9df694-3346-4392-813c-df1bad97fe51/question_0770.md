# Q0770: accrue-debt-asset via call-ststx-ratio: convert a rounding direction into a repeatable extraction

## Question
Entering through `call-ststx-ratio` (mainnet/contracts/market/v0-4-market.clar:1015) while controlling whether the ratio is fetched before or after other state changes in the block, can an unprivileged attacker make `accrue-debt-asset` (mainnet/contracts/market/v0-4-market.clar:262) convert a rounding direction into a repeatable extraction? `accrue-debt-asset` calls `accrue-and-cache` with `unwrap-panic` inside a fold whose accumulator ignores the result, so the invariant that a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction would fail, yielding protocol insolvency through uncollateralised debt.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:262` -> `accrue-debt-asset`
- Entrypoint: `call-ststx-ratio` (`mainnet/contracts/market/v0-4-market.clar:1015`), unprivileged and publicly callable
- Attacker controls: whether the ratio is fetched before or after other state changes in the block
- Exploit idea: `accrue-debt-asset` calls `accrue-and-cache` with `unwrap-panic` inside a fold whose accumulator ignores the result. Reach it through `call-ststx-ratio` and convert a rounding direction into a repeatable extraction.
- Invariant to test: a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction
- Expected Immunefi impact: Critical - protocol insolvency through uncollateralised debt
- Fast validation: Write a Clarinet simnet test calling `call-ststx-ratio` twice with whether the ratio is fetched before or after other state changes in the block varied, and assert that the value `accrue-debt-asset` returns is identical in both runs; a divergence confirms the finding.
