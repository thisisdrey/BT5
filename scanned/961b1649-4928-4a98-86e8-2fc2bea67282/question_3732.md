# Q3732: zip via repay: reach a state the guard immediately upstream of it never c

## Question
Does `repay` (mainnet/contracts/market/v0-4-market.clar:1316) let an unprivileged attacker who controls the `ft` trait principal reach `zip` (mainnet/contracts/vault/v0-vault-stx.clar:226) in a state where it reach a state the guard immediately upstream of it never contemplated? Given that it pairs the utilization and rate point lists element by element, the invariant that collateral seized equals debt repaid scaled by the penalty, and only above the liquidation LTV breaks and the result is permanent freezing of a position that can never be closed.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:226` -> `zip`
- Entrypoint: `repay` (`mainnet/contracts/market/v0-4-market.clar:1316`), unprivileged and publicly callable
- Attacker controls: the `ft` trait principal
- Exploit idea: `zip` pairs the utilization and rate point lists element by element. Reach it through `repay` and reach a state the guard immediately upstream of it never contemplated.
- Invariant to test: collateral seized equals debt repaid scaled by the penalty, and only above the liquidation LTV
- Expected Immunefi impact: Critical - permanent freezing of a position that can never be closed
- Fast validation: Fuzz the `ft` trait principal across its boundary values through `repay` in simnet and assert `zip` never returns a value that breaks the invariant.
