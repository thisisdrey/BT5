# Q0123: accrue-user-debts via repay: compose two individually correct mechanisms into an incorr

## Question
`accrue-user-debts` (mainnet/contracts/market/v0-4-market.clar:259) folds accrual over the position's debt list only. Can an unprivileged caller of `repay` (mainnet/contracts/market/v0-4-market.clar:1316), by choosing the `ft` trait principal, use that to compose two individually correct mechanisms into an incorrect result, violating the invariant that a value cached within a block still describes the state it was derived from and producing permanent freezing of a position that can never be closed?

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:259` -> `accrue-user-debts`
- Entrypoint: `repay` (`mainnet/contracts/market/v0-4-market.clar:1316`), unprivileged and publicly callable
- Attacker controls: the `ft` trait principal
- Exploit idea: `accrue-user-debts` folds accrual over the position's debt list only. Reach it through `repay` and compose two individually correct mechanisms into an incorrect result.
- Invariant to test: a value cached within a block still describes the state it was derived from
- Expected Immunefi impact: Critical - permanent freezing of a position that can never be closed
- Fast validation: Snapshot every state variable `accrue-user-debts` touches, run `repay` with the `ft` trait principal, recompute the invariant off-chain from the snapshot, and assert it matches the on-chain result.
