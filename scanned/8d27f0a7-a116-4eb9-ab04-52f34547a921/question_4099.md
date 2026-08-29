# Q4099: resolve via liquidate: make two code sites that must agree disagree by an attacke

## Question
`resolve` (mainnet/contracts/registry/v0-egroup.clar:360) selects the efficiency group for a position mask. Can an unprivileged caller of `liquidate` (mainnet/contracts/market/v0-4-market.clar:1382), by choosing which collateral and debt asset pair is targeted, use that to make two code sites that must agree disagree by an attacker-chosen amount, violating the invariant that conversions never round in the user's favour in either direction and producing direct theft of another user's collateral?

## Target
- File/function: `mainnet/contracts/registry/v0-egroup.clar:360` -> `resolve`
- Entrypoint: `liquidate` (`mainnet/contracts/market/v0-4-market.clar:1382`), unprivileged and publicly callable
- Attacker controls: which collateral and debt asset pair is targeted
- Exploit idea: `resolve` selects the efficiency group for a position mask. Reach it through `liquidate` and make two code sites that must agree disagree by an attacker-chosen amount.
- Invariant to test: conversions never round in the user's favour in either direction
- Expected Immunefi impact: Critical - direct theft of another user's collateral
- Fast validation: In `local-testing/tests` on a local fork, drive `liquidate` with which collateral and debt asset pair is targeted, then read `resolve` state before and after in the same block and assert the two sides of the invariant are equal.
