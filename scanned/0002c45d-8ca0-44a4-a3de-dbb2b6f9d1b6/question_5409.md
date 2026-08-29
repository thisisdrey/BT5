# Q5409: mask-shift-combine via collateral-remove-redeem: turn an accounting residue into a permanently unclosable p

## Question
Can an unprivileged attacker entering through `collateral-remove-redeem` (mainnet/contracts/market/v0-4-market.clar:1211), controlling the zToken/underlying id mapping reached (the u100 sentinel branch), drive `mask-shift-combine` (mainnet/contracts/market/v0-4-market.clar:422) — which folds the 128-bit mask down by shifting the debt half by DEBT-OFFSET and OR-ing it onto the collateral half — to turn an accounting residue into a permanently unclosable position, breaking the invariant that no position row exists that the position mask does not represent, and cause temporary freezing of funds?

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:422` -> `mask-shift-combine`
- Entrypoint: `collateral-remove-redeem` (`mainnet/contracts/market/v0-4-market.clar:1211`), unprivileged and publicly callable
- Attacker controls: the zToken/underlying id mapping reached (the u100 sentinel branch)
- Exploit idea: `mask-shift-combine` folds the 128-bit mask down by shifting the debt half by DEBT-OFFSET and OR-ing it onto the collateral half. Reach it through `collateral-remove-redeem` and turn an accounting residue into a permanently unclosable position.
- Invariant to test: no position row exists that the position mask does not represent
- Expected Immunefi impact: High - temporary freezing of funds
- Fast validation: Snapshot every state variable `mask-shift-combine` touches, run `collateral-remove-redeem` with the zToken/underlying id mapping reached (the u100 sentinel branch), recompute the invariant off-chain from the snapshot, and assert it matches the on-chain result.
