# Q4569: vault-system-borrow via liquidate: compose two individually correct mechanisms into an incorr

## Question
Can an unprivileged attacker entering through `liquidate` (mainnet/contracts/market/v0-4-market.clar:1382), controlling `borrower`, any third-party principal, drive `vault-system-borrow` (mainnet/contracts/market/v0-4-market.clar:198) — which routes a borrow to one of six vaults by asset id — to compose two individually correct mechanisms into an incorrect result, breaking the invariant that collateral seized equals debt repaid scaled by the penalty, and only above the liquidation LTV, and cause permanent freezing of a position that can never be closed?

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:198` -> `vault-system-borrow`
- Entrypoint: `liquidate` (`mainnet/contracts/market/v0-4-market.clar:1382`), unprivileged and publicly callable
- Attacker controls: `borrower`, any third-party principal
- Exploit idea: `vault-system-borrow` routes a borrow to one of six vaults by asset id. Reach it through `liquidate` and compose two individually correct mechanisms into an incorrect result.
- Invariant to test: collateral seized equals debt repaid scaled by the penalty, and only above the liquidation LTV
- Expected Immunefi impact: Critical - permanent freezing of a position that can never be closed
- Fast validation: Snapshot every state variable `vault-system-borrow` touches, run `liquidate` with `borrower`, any third-party principal, recompute the invariant off-chain from the snapshot, and assert it matches the on-chain result.
