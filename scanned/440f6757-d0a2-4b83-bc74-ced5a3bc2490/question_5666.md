# Q5666: calculate-asset-notional-value via borrow: make an aggregate and its per-item breakdown disagree

## Question
Entering through `borrow` (mainnet/contracts/market/v0-4-market.clar:1238) while controlling `amount`, can an unprivileged attacker make `calculate-asset-notional-value` (mainnet/contracts/market/v0-4-market.clar:544) make an aggregate and its per-item breakdown disagree? `calculate-asset-notional-value` normalizes collateral with round-down and debt with round-up, and calls `accrue-and-cache` with `unwrap-panic` inside the fold, so the invariant that only the acting principal's own position is mutated would fail, yielding protocol insolvency through uncollateralised debt.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:544` -> `calculate-asset-notional-value`
- Entrypoint: `borrow` (`mainnet/contracts/market/v0-4-market.clar:1238`), unprivileged and publicly callable
- Attacker controls: `amount`
- Exploit idea: `calculate-asset-notional-value` normalizes collateral with round-down and debt with round-up, and calls `accrue-and-cache` with `unwrap-panic` inside the fold. Reach it through `borrow` and make an aggregate and its per-item breakdown disagree.
- Invariant to test: only the acting principal's own position is mutated
- Expected Immunefi impact: Critical - protocol insolvency through uncollateralised debt
- Fast validation: Write a Clarinet simnet test calling `borrow` twice with `amount` varied, and assert that the value `calculate-asset-notional-value` returns is identical in both runs; a divergence confirms the finding.
