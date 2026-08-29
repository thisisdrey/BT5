# Q3608: interest-rate via deposit: reach a state the guard immediately upstream of it never c

## Question
Does `deposit` (mainnet/contracts/vault/v0-vault-stx.clar:763) let an unprivileged attacker who controls `amount` reach `interest-rate` (mainnet/contracts/vault/v0-vault-stx.clar:371) in a state where it reach a state the guard immediately upstream of it never contemplated? Given that it interpolates the packed curve at the current utilization, the invariant that only the acting principal's own position is mutated breaks and the result is permanent freezing of unclaimed yield.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:371` -> `interest-rate`
- Entrypoint: `deposit` (`mainnet/contracts/vault/v0-vault-stx.clar:763`), unprivileged and publicly callable
- Attacker controls: `amount`
- Exploit idea: `interest-rate` interpolates the packed curve at the current utilization. Reach it through `deposit` and reach a state the guard immediately upstream of it never contemplated.
- Invariant to test: only the acting principal's own position is mutated
- Expected Immunefi impact: High - permanent freezing of unclaimed yield
- Fast validation: Write a Clarinet simnet test calling `deposit` twice with `amount` varied, and assert that the value `interest-rate` returns is identical in both runs; a divergence confirms the finding.
