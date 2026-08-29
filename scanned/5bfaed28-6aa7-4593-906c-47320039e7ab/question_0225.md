# Q0225: get-asset-value via liquidate: make two code sites that must agree disagree by an attacke

## Question
Can an unprivileged attacker entering through `liquidate` (mainnet/contracts/market/v0-4-market.clar:1382), controlling `min-collateral-expected`, drive `get-asset-value` (mainnet/contracts/market/v0-4-market.clar:679) — which resolves a fresh price for a single asset and normalizes with a caller-supplied rounding direction — to make two code sites that must agree disagree by an attacker-chosen amount, breaking the invariant that only the acting principal's own position is mutated, and cause permanent freezing of funds?

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:679` -> `get-asset-value`
- Entrypoint: `liquidate` (`mainnet/contracts/market/v0-4-market.clar:1382`), unprivileged and publicly callable
- Attacker controls: `min-collateral-expected`
- Exploit idea: `get-asset-value` resolves a fresh price for a single asset and normalizes with a caller-supplied rounding direction. Reach it through `liquidate` and make two code sites that must agree disagree by an attacker-chosen amount.
- Invariant to test: only the acting principal's own position is mutated
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Snapshot every state variable `get-asset-value` touches, run `liquidate` with `min-collateral-expected`, recompute the invariant off-chain from the snapshot, and assert it matches the on-chain result.
