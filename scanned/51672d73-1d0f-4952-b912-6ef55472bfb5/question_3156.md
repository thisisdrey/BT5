# Q3156: remove-user-scaled-debt via liquidate: reach a state the guard immediately upstream of it never c

## Question
Does `liquidate` (mainnet/contracts/market/v0-4-market.clar:1382) let an unprivileged attacker who controls `min-collateral-expected` reach `remove-user-scaled-debt` (mainnet/contracts/market/v0-market-vault.clar:244) in a state where it reach a state the guard immediately upstream of it never contemplated? Given that it deletes the row only on an exact zero, otherwise leaving a residue that keeps the mask bit set, the invariant that collateral seized equals debt repaid scaled by the penalty, and only above the liquidation LTV breaks and the result is permanent freezing of a position that can never be closed.

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:244` -> `remove-user-scaled-debt`
- Entrypoint: `liquidate` (`mainnet/contracts/market/v0-4-market.clar:1382`), unprivileged and publicly callable
- Attacker controls: `min-collateral-expected`
- Exploit idea: `remove-user-scaled-debt` deletes the row only on an exact zero, otherwise leaving a residue that keeps the mask bit set. Reach it through `liquidate` and reach a state the guard immediately upstream of it never contemplated.
- Invariant to test: collateral seized equals debt repaid scaled by the penalty, and only above the liquidation LTV
- Expected Immunefi impact: Critical - permanent freezing of a position that can never be closed
- Fast validation: Fuzz `min-collateral-expected` across its boundary values through `liquidate` in simnet and assert `remove-user-scaled-debt` never returns a value that breaks the invariant.
