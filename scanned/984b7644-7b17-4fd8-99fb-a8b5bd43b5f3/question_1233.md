# Q1233: vault-system-repay via repay: reach a state the guard immediately upstream of it never c

## Question
Can an unprivileged attacker entering through `repay` (mainnet/contracts/market/v0-4-market.clar:1316), controlling `on-behalf-of`, naming any third-party principal, drive `vault-system-repay` (mainnet/contracts/market/v0-4-market.clar:207) — which routes a repayment to one of six vaults by asset id — to reach a state the guard immediately upstream of it never contemplated, breaking the invariant that a vault's underlying plus outstanding debt covers all shares and all supplier claims, and cause permanent freezing of funds?

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:207` -> `vault-system-repay`
- Entrypoint: `repay` (`mainnet/contracts/market/v0-4-market.clar:1316`), unprivileged and publicly callable
- Attacker controls: `on-behalf-of`, naming any third-party principal
- Exploit idea: `vault-system-repay` routes a repayment to one of six vaults by asset id. Reach it through `repay` and reach a state the guard immediately upstream of it never contemplated.
- Invariant to test: a vault's underlying plus outstanding debt covers all shares and all supplier claims
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Snapshot every state variable `vault-system-repay` touches, run `repay` with `on-behalf-of`, naming any third-party principal, recompute the invariant off-chain from the snapshot, and assert it matches the on-chain result.
