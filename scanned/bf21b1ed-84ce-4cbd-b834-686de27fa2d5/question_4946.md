# Q4946: check-confidence via borrow: make an aggregate and its per-item breakdown disagree

## Question
Entering through `borrow` (mainnet/contracts/market/v0-4-market.clar:1238) while controlling `amount`, can an unprivileged attacker make `check-confidence` (mainnet/contracts/market/v0-4-market.clar:305) make an aggregate and its per-item breakdown disagree? `check-confidence` compares the Pyth confidence interval against `max-confidence-ratio` in BPS, a gate that has no DIA equivalent, so the invariant that only the acting principal's own position is mutated would fail, yielding protocol insolvency through uncollateralised debt.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:305` -> `check-confidence`
- Entrypoint: `borrow` (`mainnet/contracts/market/v0-4-market.clar:1238`), unprivileged and publicly callable
- Attacker controls: `amount`
- Exploit idea: `check-confidence` compares the Pyth confidence interval against `max-confidence-ratio` in BPS, a gate that has no DIA equivalent. Reach it through `borrow` and make an aggregate and its per-item breakdown disagree.
- Invariant to test: only the acting principal's own position is mutated
- Expected Immunefi impact: Critical - protocol insolvency through uncollateralised debt
- Fast validation: Write a Clarinet simnet test calling `borrow` twice with `amount` varied, and assert that the value `check-confidence` returns is identical in both runs; a divergence confirms the finding.
