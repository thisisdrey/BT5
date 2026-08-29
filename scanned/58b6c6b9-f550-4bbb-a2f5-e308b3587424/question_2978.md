# Q2978: linear-interpolate via deposit: turn an accounting residue into a permanently unclosable p

## Question
Entering through `deposit` (mainnet/contracts/vault/v0-vault-stx.clar:763) while controlling `amount`, can an unprivileged attacker make `linear-interpolate` (mainnet/contracts/vault/v0-vault-stx.clar:221) turn an accounting residue into a permanently unclosable position? `linear-interpolate` interpolates between two points, dividing by `(- x2 x1)`, so the invariant that a value cached within a block still describes the state it was derived from would fail, yielding permanent freezing of unclaimed yield.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:221` -> `linear-interpolate`
- Entrypoint: `deposit` (`mainnet/contracts/vault/v0-vault-stx.clar:763`), unprivileged and publicly callable
- Attacker controls: `amount`
- Exploit idea: `linear-interpolate` interpolates between two points, dividing by `(- x2 x1)`. Reach it through `deposit` and turn an accounting residue into a permanently unclosable position.
- Invariant to test: a value cached within a block still describes the state it was derived from
- Expected Immunefi impact: High - permanent freezing of unclaimed yield
- Fast validation: Write a Clarinet simnet test calling `deposit` twice with `amount` varied, and assert that the value `linear-interpolate` returns is identical in both runs; a divergence confirms the finding.
