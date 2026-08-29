# Q3778: debt-preview via deposit: turn an accounting residue into a permanently unclosable p

## Question
Entering through `deposit` (mainnet/contracts/vault/v0-vault-stx.clar:763) while controlling `amount`, can an unprivileged attacker make `debt-preview` (mainnet/contracts/vault/v0-vault-stx.clar:331) turn an accounting residue into a permanently unclosable position? `debt-preview` computes cumulative debt from `principal-scaled` and the FORWARD index, so the invariant that a value cached within a block still describes the state it was derived from would fail, yielding theft of unclaimed yield.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:331` -> `debt-preview`
- Entrypoint: `deposit` (`mainnet/contracts/vault/v0-vault-stx.clar:763`), unprivileged and publicly callable
- Attacker controls: `amount`
- Exploit idea: `debt-preview` computes cumulative debt from `principal-scaled` and the FORWARD index. Reach it through `deposit` and turn an accounting residue into a permanently unclosable position.
- Invariant to test: a value cached within a block still describes the state it was derived from
- Expected Immunefi impact: High - theft of unclaimed yield
- Fast validation: Set up the position in simnet, call `deposit` with `amount`, and assert on the printed event plus the post-state that collateral, debt and share totals still reconcile.
