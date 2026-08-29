# Q3183: vault-accrue via borrow: make an aggregate and its per-item breakdown disagree

## Question
`vault-accrue` (mainnet/contracts/market/v0-4-market.clar:189) dispatches accrual to one of six vaults by asset id. Can an unprivileged caller of `borrow` (mainnet/contracts/market/v0-4-market.clar:1238), by choosing the order of accrual versus price resolution inside the let, use that to make an aggregate and its per-item breakdown disagree, violating the invariant that no position row exists that the position mask does not represent and producing permanent freezing of a position that can never be closed?

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:189` -> `vault-accrue`
- Entrypoint: `borrow` (`mainnet/contracts/market/v0-4-market.clar:1238`), unprivileged and publicly callable
- Attacker controls: the order of accrual versus price resolution inside the let
- Exploit idea: `vault-accrue` dispatches accrual to one of six vaults by asset id. Reach it through `borrow` and make an aggregate and its per-item breakdown disagree.
- Invariant to test: no position row exists that the position mask does not represent
- Expected Immunefi impact: Critical - permanent freezing of a position that can never be closed
- Fast validation: Snapshot every state variable `vault-accrue` touches, run `borrow` with the order of accrual versus price resolution inside the let, recompute the invariant off-chain from the snapshot, and assert it matches the on-chain result.
