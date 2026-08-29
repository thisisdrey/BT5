# Q5877: mask-shift-combine via repay: satisfy a bound with a value the bound was never designed 

## Question
Can an unprivileged attacker entering through `repay` (mainnet/contracts/market/v0-4-market.clar:1316), controlling the `ft` trait principal, drive `mask-shift-combine` (mainnet/contracts/market/v0-4-market.clar:422) — which folds the 128-bit mask down by shifting the debt half by DEBT-OFFSET and OR-ing it onto the collateral half — to satisfy a bound with a value the bound was never designed to admit, breaking the invariant that conversions never round in the user's favour in either direction, and cause permanent freezing of funds?

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:422` -> `mask-shift-combine`
- Entrypoint: `repay` (`mainnet/contracts/market/v0-4-market.clar:1316`), unprivileged and publicly callable
- Attacker controls: the `ft` trait principal
- Exploit idea: `mask-shift-combine` folds the 128-bit mask down by shifting the debt half by DEBT-OFFSET and OR-ing it onto the collateral half. Reach it through `repay` and satisfy a bound with a value the bound was never designed to admit.
- Invariant to test: conversions never round in the user's favour in either direction
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Snapshot every state variable `mask-shift-combine` touches, run `repay` with the `ft` trait principal, recompute the invariant off-chain from the snapshot, and assert it matches the on-chain result.
