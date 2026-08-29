# Q4373: mask-shift-combine via liquidate: compose two individually correct mechanisms into an incorr

## Question
Can an unprivileged attacker entering through `liquidate` (mainnet/contracts/market/v0-4-market.clar:1382), controlling `min-collateral-expected`, drive `mask-shift-combine` (mainnet/contracts/market/v0-4-market.clar:422) — which folds the 128-bit mask down by shifting the debt half by DEBT-OFFSET and OR-ing it onto the collateral half — to compose two individually correct mechanisms into an incorrect result, breaking the invariant that only the acting principal's own position is mutated, and cause protocol insolvency through uncollateralised debt?

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:422` -> `mask-shift-combine`
- Entrypoint: `liquidate` (`mainnet/contracts/market/v0-4-market.clar:1382`), unprivileged and publicly callable
- Attacker controls: `min-collateral-expected`
- Exploit idea: `mask-shift-combine` folds the 128-bit mask down by shifting the debt half by DEBT-OFFSET and OR-ing it onto the collateral half. Reach it through `liquidate` and compose two individually correct mechanisms into an incorrect result.
- Invariant to test: only the acting principal's own position is mutated
- Expected Immunefi impact: Critical - protocol insolvency through uncollateralised debt
- Fast validation: Run the baseline `liquidate` call, then the attacker-shaped one with `min-collateral-expected`, and assert the attacker's net token balance change is zero or negative.
