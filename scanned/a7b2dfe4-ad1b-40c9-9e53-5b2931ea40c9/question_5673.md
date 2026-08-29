# Q5673: get-egroup via repay: turn an accounting residue into a permanently unclosable p

## Question
Can an unprivileged attacker entering through `repay` (mainnet/contracts/market/v0-4-market.clar:1316), controlling `on-behalf-of`, naming any third-party principal, drive `get-egroup` (mainnet/contracts/market/v0-4-market.clar:460) — which resolves the efficiency group for a mask and is unwrapped with `try!` on every health path — to turn an accounting residue into a permanently unclosable position, breaking the invariant that no position row exists that the position mask does not represent, and cause permanent freezing of funds?

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:460` -> `get-egroup`
- Entrypoint: `repay` (`mainnet/contracts/market/v0-4-market.clar:1316`), unprivileged and publicly callable
- Attacker controls: `on-behalf-of`, naming any third-party principal
- Exploit idea: `get-egroup` resolves the efficiency group for a mask and is unwrapped with `try!` on every health path. Reach it through `repay` and turn an accounting residue into a permanently unclosable position.
- Invariant to test: no position row exists that the position mask does not represent
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Snapshot every state variable `get-egroup` touches, run `repay` with `on-behalf-of`, naming any third-party principal, recompute the invariant off-chain from the snapshot, and assert it matches the on-chain result.
