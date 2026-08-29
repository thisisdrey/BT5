# Q2561: status via collateral-add: convert a rounding direction into a repeatable extraction

## Question
Can an unprivileged attacker entering through `collateral-add` (mainnet/contracts/market/v0-4-market.clar:1020), controlling the `ft` trait principal, drive `status` (mainnet/contracts/registry/v0-assets.clar:115) — which derives `collateral` and `debt` flags from bit tests against whatever mask it was handed — to convert a rounding direction into a repeatable extraction, breaking the invariant that conversions never round in the user's favour in either direction, and cause protocol insolvency through uncollateralised debt?

## Target
- File/function: `mainnet/contracts/registry/v0-assets.clar:115` -> `status`
- Entrypoint: `collateral-add` (`mainnet/contracts/market/v0-4-market.clar:1020`), unprivileged and publicly callable
- Attacker controls: the `ft` trait principal
- Exploit idea: `status` derives `collateral` and `debt` flags from bit tests against whatever mask it was handed. Reach it through `collateral-add` and convert a rounding direction into a repeatable extraction.
- Invariant to test: conversions never round in the user's favour in either direction
- Expected Immunefi impact: Critical - protocol insolvency through uncollateralised debt
- Fast validation: Run the baseline `collateral-add` call, then the attacker-shaped one with the `ft` trait principal, and assert the attacker's net token balance change is zero or negative.
