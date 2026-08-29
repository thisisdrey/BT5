# Q3704: principal-ratio-reduction via transfer: reach a state the guard immediately upstream of it never c

## Question
Does `transfer` (mainnet/contracts/vault/v0-vault-stx.clar:752) let an unprivileged attacker who controls `amount` reach `principal-ratio-reduction` (mainnet/contracts/vault/v0-vault-stx.clar:406) in a state where it reach a state the guard immediately upstream of it never contemplated? Given that it derives a principal reduction from an amount, the scaled principal and the previewed debt, the invariant that only the acting principal's own position is mutated breaks and the result is protocol insolvency through uncollateralised debt.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:406` -> `principal-ratio-reduction`
- Entrypoint: `transfer` (`mainnet/contracts/vault/v0-vault-stx.clar:752`), unprivileged and publicly callable
- Attacker controls: `amount`
- Exploit idea: `principal-ratio-reduction` derives a principal reduction from an amount, the scaled principal and the previewed debt. Reach it through `transfer` and reach a state the guard immediately upstream of it never contemplated.
- Invariant to test: only the acting principal's own position is mutated
- Expected Immunefi impact: Critical - protocol insolvency through uncollateralised debt
- Fast validation: Write a Clarinet simnet test calling `transfer` twice with `amount` varied, and assert that the value `principal-ratio-reduction` returns is identical in both runs; a divergence confirms the finding.
