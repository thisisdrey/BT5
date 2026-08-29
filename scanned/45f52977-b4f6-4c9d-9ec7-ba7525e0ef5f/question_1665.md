# Q1665: vault-system-repay via borrow: reach a state the guard immediately upstream of it never c

## Question
Can an unprivileged attacker entering through `borrow` (mainnet/contracts/market/v0-4-market.clar:1238), controlling the `price-feeds` buffers, drive `vault-system-repay` (mainnet/contracts/market/v0-4-market.clar:207) — which routes a repayment to one of six vaults by asset id — to reach a state the guard immediately upstream of it never contemplated, breaking the invariant that a vault's underlying plus outstanding debt covers all shares and all supplier claims, and cause permanent freezing of funds?

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:207` -> `vault-system-repay`
- Entrypoint: `borrow` (`mainnet/contracts/market/v0-4-market.clar:1238`), unprivileged and publicly callable
- Attacker controls: the `price-feeds` buffers
- Exploit idea: `vault-system-repay` routes a repayment to one of six vaults by asset id. Reach it through `borrow` and reach a state the guard immediately upstream of it never contemplated.
- Invariant to test: a vault's underlying plus outstanding debt covers all shares and all supplier claims
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Snapshot every state variable `vault-system-repay` touches, run `borrow` with the `price-feeds` buffers, recompute the invariant off-chain from the snapshot, and assert it matches the on-chain result.
