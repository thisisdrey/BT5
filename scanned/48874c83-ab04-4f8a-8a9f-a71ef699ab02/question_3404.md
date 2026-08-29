# Q3404: add-user-collateral via transfer: reach a state the guard immediately upstream of it never c

## Question
Does `transfer` (mainnet/contracts/vault/v0-vault-stx.clar:752) let an unprivileged attacker who controls `amount` reach `add-user-collateral` (mainnet/contracts/market/v0-market-vault.clar:198) in a state where it reach a state the guard immediately upstream of it never contemplated? Given that it adds to the collateral row with a graceful u0 default, the invariant that collateral seized equals debt repaid scaled by the penalty, and only above the liquidation LTV breaks and the result is protocol insolvency.

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:198` -> `add-user-collateral`
- Entrypoint: `transfer` (`mainnet/contracts/vault/v0-vault-stx.clar:752`), unprivileged and publicly callable
- Attacker controls: `amount`
- Exploit idea: `add-user-collateral` adds to the collateral row with a graceful u0 default. Reach it through `transfer` and reach a state the guard immediately upstream of it never contemplated.
- Invariant to test: collateral seized equals debt repaid scaled by the penalty, and only above the liquidation LTV
- Expected Immunefi impact: Critical - protocol insolvency
- Fast validation: Write a Clarinet simnet test calling `transfer` twice with `amount` varied, and assert that the value `add-user-collateral` returns is identical in both runs; a divergence confirms the finding.
