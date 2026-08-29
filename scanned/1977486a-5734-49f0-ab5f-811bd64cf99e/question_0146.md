# Q0146: unwrap-status via liquidate: convert a rounding direction into a repeatable extraction

## Question
Entering through `liquidate` (mainnet/contracts/market/v0-4-market.clar:1382) while controlling `debt-amount`, can an unprivileged attacker make `unwrap-status` (mainnet/contracts/registry/v0-assets.clar:111) convert a rounding direction into a repeatable extraction? `unwrap-status` resolves `status` with `unwrap-panic`, so the invariant that a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction would fail, yielding protocol insolvency.

## Target
- File/function: `mainnet/contracts/registry/v0-assets.clar:111` -> `unwrap-status`
- Entrypoint: `liquidate` (`mainnet/contracts/market/v0-4-market.clar:1382`), unprivileged and publicly callable
- Attacker controls: `debt-amount`
- Exploit idea: `unwrap-status` resolves `status` with `unwrap-panic`. Reach it through `liquidate` and convert a rounding direction into a repeatable extraction.
- Invariant to test: a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction
- Expected Immunefi impact: Critical - protocol insolvency
- Fast validation: Write a Clarinet simnet test calling `liquidate` twice with `debt-amount` varied, and assert that the value `unwrap-status` returns is identical in both runs; a divergence confirms the finding.
