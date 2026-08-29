# Q1472: total-assets via transfer: make an aggregate and its per-item breakdown disagree

## Question
Does `transfer` (mainnet/contracts/vault/v0-vault-stx.clar:752) let an unprivileged attacker who controls the timing relative to a pledge or a liquidation reach `total-assets` (mainnet/contracts/vault/v0-vault-stx.clar:334) in a state where it make an aggregate and its per-item breakdown disagree? Given that it adds `(- debt borrowed)` as accrued interest that no token in the vault yet backs, the invariant that a value cached within a block still describes the state it was derived from breaks and the result is protocol insolvency through uncollateralised debt.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:334` -> `total-assets`
- Entrypoint: `transfer` (`mainnet/contracts/vault/v0-vault-stx.clar:752`), unprivileged and publicly callable
- Attacker controls: the timing relative to a pledge or a liquidation
- Exploit idea: `total-assets` adds `(- debt borrowed)` as accrued interest that no token in the vault yet backs. Reach it through `transfer` and make an aggregate and its per-item breakdown disagree.
- Invariant to test: a value cached within a block still describes the state it was derived from
- Expected Immunefi impact: Critical - protocol insolvency through uncollateralised debt
- Fast validation: Write a Clarinet simnet test calling `transfer` twice with the timing relative to a pledge or a liquidation varied, and assert that the value `total-assets` returns is identical in both runs; a divergence confirms the finding.
