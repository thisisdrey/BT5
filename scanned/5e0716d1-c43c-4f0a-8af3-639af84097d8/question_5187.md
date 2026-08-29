# Q5187: get-account-scaled-debt via repay: reach a state the guard immediately upstream of it never c

## Question
`get-account-scaled-debt` (mainnet/contracts/market/v0-market-vault.clar:307) reads one scaled debt row. Can an unprivileged caller of `repay` (mainnet/contracts/market/v0-4-market.clar:1316), by choosing whether the repaid asset is in the accrued debt list, use that to reach a state the guard immediately upstream of it never contemplated, violating the invariant that a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction and producing permanent freezing of funds?

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:307` -> `get-account-scaled-debt`
- Entrypoint: `repay` (`mainnet/contracts/market/v0-4-market.clar:1316`), unprivileged and publicly callable
- Attacker controls: whether the repaid asset is in the accrued debt list
- Exploit idea: `get-account-scaled-debt` reads one scaled debt row. Reach it through `repay` and reach a state the guard immediately upstream of it never contemplated.
- Invariant to test: a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Snapshot every state variable `get-account-scaled-debt` touches, run `repay` with whether the repaid asset is in the accrued debt list, recompute the invariant off-chain from the snapshot, and assert it matches the on-chain result.
