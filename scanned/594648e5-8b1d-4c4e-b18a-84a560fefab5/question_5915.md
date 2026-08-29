# Q5915: linear-interpolate via deposit: convert a rounding direction into a repeatable extraction

## Question
`linear-interpolate` (mainnet/contracts/vault/v0-vault-stx.clar:221) interpolates between two points, dividing by `(- x2 x1)`. Can an unprivileged caller of `deposit` (mainnet/contracts/vault/v0-vault-stx.clar:763), by choosing `amount`, use that to convert a rounding direction into a repeatable extraction, violating the invariant that collateral seized equals debt repaid scaled by the penalty, and only above the liquidation LTV and producing protocol insolvency?

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:221` -> `linear-interpolate`
- Entrypoint: `deposit` (`mainnet/contracts/vault/v0-vault-stx.clar:763`), unprivileged and publicly callable
- Attacker controls: `amount`
- Exploit idea: `linear-interpolate` interpolates between two points, dividing by `(- x2 x1)`. Reach it through `deposit` and convert a rounding direction into a repeatable extraction.
- Invariant to test: collateral seized equals debt repaid scaled by the penalty, and only above the liquidation LTV
- Expected Immunefi impact: Critical - protocol insolvency
- Fast validation: Run the baseline `deposit` call, then the attacker-shaped one with `amount`, and assert the attacker's net token balance change is zero or negative.
