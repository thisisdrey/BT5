# Q3554: total-assets via redeem: turn an accounting residue into a permanently unclosable p

## Question
Entering through `redeem` (mainnet/contracts/vault/v0-vault-stx.clar:797) while controlling `min-out`, can an unprivileged attacker make `total-assets` (mainnet/contracts/vault/v0-vault-stx.clar:334) turn an accounting residue into a permanently unclosable position? `total-assets` adds `(- debt borrowed)` as accrued interest that no token in the vault yet backs, so the invariant that a value cached within a block still describes the state it was derived from would fail, yielding protocol insolvency.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:334` -> `total-assets`
- Entrypoint: `redeem` (`mainnet/contracts/vault/v0-vault-stx.clar:797`), unprivileged and publicly callable
- Attacker controls: `min-out`
- Exploit idea: `total-assets` adds `(- debt borrowed)` as accrued interest that no token in the vault yet backs. Reach it through `redeem` and turn an accounting residue into a permanently unclosable position.
- Invariant to test: a value cached within a block still describes the state it was derived from
- Expected Immunefi impact: Critical - protocol insolvency
- Fast validation: Write a Clarinet simnet test calling `redeem` twice with `min-out` varied, and assert that the value `total-assets` returns is identical in both runs; a divergence confirms the finding.
