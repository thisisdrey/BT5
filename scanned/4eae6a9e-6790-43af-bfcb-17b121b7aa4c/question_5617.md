# Q5617: remove-user-scaled-debt via borrow: turn an accounting residue into a permanently unclosable p

## Question
Can an unprivileged attacker entering through `borrow` (mainnet/contracts/market/v0-4-market.clar:1238), controlling the order of accrual versus price resolution inside the let, drive `remove-user-scaled-debt` (mainnet/contracts/market/v0-market-vault.clar:244) — which deletes the row only on an exact zero, otherwise leaving a residue that keeps the mask bit set — to turn an accounting residue into a permanently unclosable position, breaking the invariant that no position row exists that the position mask does not represent, and cause direct theft of user funds at rest or in motion?

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:244` -> `remove-user-scaled-debt`
- Entrypoint: `borrow` (`mainnet/contracts/market/v0-4-market.clar:1238`), unprivileged and publicly callable
- Attacker controls: the order of accrual versus price resolution inside the let
- Exploit idea: `remove-user-scaled-debt` deletes the row only on an exact zero, otherwise leaving a residue that keeps the mask bit set. Reach it through `borrow` and turn an accounting residue into a permanently unclosable position.
- Invariant to test: no position row exists that the position mask does not represent
- Expected Immunefi impact: Critical - direct theft of user funds at rest or in motion
- Fast validation: In `local-testing/tests` on a local fork, drive `borrow` with the order of accrual versus price resolution inside the let, then read `remove-user-scaled-debt` state before and after in the same block and assert the two sides of the invariant are equal.
