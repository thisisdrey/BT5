# Q5960: resolve-pyth via collateral-add: compose two individually correct mechanisms into an incorr

## Question
Does `collateral-add` (mainnet/contracts/market/v0-4-market.clar:1020) let an unprivileged attacker who controls `amount` reach `resolve-pyth` (mainnet/contracts/market/v0-4-market.clar:312) in a state where it compose two individually correct mechanisms into an incorrect result? Given that it reads the Pyth storage record for a 32-byte ident, the invariant that a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction breaks and the result is protocol insolvency.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:312` -> `resolve-pyth`
- Entrypoint: `collateral-add` (`mainnet/contracts/market/v0-4-market.clar:1020`), unprivileged and publicly callable
- Attacker controls: `amount`
- Exploit idea: `resolve-pyth` reads the Pyth storage record for a 32-byte ident. Reach it through `collateral-add` and compose two individually correct mechanisms into an incorrect result.
- Invariant to test: a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction
- Expected Immunefi impact: Critical - protocol insolvency
- Fast validation: Write a Clarinet simnet test calling `collateral-add` twice with `amount` varied, and assert that the value `resolve-pyth` returns is identical in both runs; a divergence confirms the finding.
