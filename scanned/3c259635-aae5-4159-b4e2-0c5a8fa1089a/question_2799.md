# Q2799: debt-preview via deposit: satisfy a bound with a value the bound was never designed 

## Question
`debt-preview` (mainnet/contracts/vault/v0-vault-stx.clar:331) computes cumulative debt from `principal-scaled` and the FORWARD index. Can an unprivileged caller of `deposit` (mainnet/contracts/vault/v0-vault-stx.clar:763), by choosing `amount`, use that to satisfy a bound with a value the bound was never designed to admit, violating the invariant that collateral seized equals debt repaid scaled by the penalty, and only above the liquidation LTV and producing temporary freezing of funds?

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:331` -> `debt-preview`
- Entrypoint: `deposit` (`mainnet/contracts/vault/v0-vault-stx.clar:763`), unprivileged and publicly callable
- Attacker controls: `amount`
- Exploit idea: `debt-preview` computes cumulative debt from `principal-scaled` and the FORWARD index. Reach it through `deposit` and satisfy a bound with a value the bound was never designed to admit.
- Invariant to test: collateral seized equals debt repaid scaled by the penalty, and only above the liquidation LTV
- Expected Immunefi impact: High - temporary freezing of funds
- Fast validation: Snapshot every state variable `debt-preview` touches, run `deposit` with `amount`, recompute the invariant off-chain from the snapshot, and assert it matches the on-chain result.
