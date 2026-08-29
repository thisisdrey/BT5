# Q3352: remove-user-scaled-debt via liquidate-multi: reach a state the guard immediately upstream of it never c

## Question
Does `liquidate-multi` (mainnet/contracts/market/v0-4-market.clar:1593) let an unprivileged attacker who controls which borrowers are placed early versus late in the batch reach `remove-user-scaled-debt` (mainnet/contracts/market/v0-market-vault.clar:244) in a state where it reach a state the guard immediately upstream of it never contemplated? Given that it deletes the row only on an exact zero, otherwise leaving a residue that keeps the mask bit set, the invariant that only the acting principal's own position is mutated breaks and the result is direct theft of another user's collateral.

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:244` -> `remove-user-scaled-debt`
- Entrypoint: `liquidate-multi` (`mainnet/contracts/market/v0-4-market.clar:1593`), unprivileged and publicly callable
- Attacker controls: which borrowers are placed early versus late in the batch
- Exploit idea: `remove-user-scaled-debt` deletes the row only on an exact zero, otherwise leaving a residue that keeps the mask bit set. Reach it through `liquidate-multi` and reach a state the guard immediately upstream of it never contemplated.
- Invariant to test: only the acting principal's own position is mutated
- Expected Immunefi impact: Critical - direct theft of another user's collateral
- Fast validation: Set up the position in simnet, call `liquidate-multi` with which borrowers are placed early versus late in the batch, and assert on the printed event plus the post-state that collateral, debt and share totals still reconcile.
