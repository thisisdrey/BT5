# Q4607: receive-tokens via collateral-remove: make two code sites that must agree disagree by an attacke

## Question
`receive-tokens` (mainnet/contracts/market/v0-market-vault.clar:256) pulls an asset from a named account. Can an unprivileged caller of `collateral-remove` (mainnet/contracts/market/v0-4-market.clar:1107), by choosing the `ft` trait principal, use that to make two code sites that must agree disagree by an attacker-chosen amount, violating the invariant that a value cached within a block still describes the state it was derived from and producing protocol insolvency through uncollateralised debt?

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:256` -> `receive-tokens`
- Entrypoint: `collateral-remove` (`mainnet/contracts/market/v0-4-market.clar:1107`), unprivileged and publicly callable
- Attacker controls: the `ft` trait principal
- Exploit idea: `receive-tokens` pulls an asset from a named account. Reach it through `collateral-remove` and make two code sites that must agree disagree by an attacker-chosen amount.
- Invariant to test: a value cached within a block still describes the state it was derived from
- Expected Immunefi impact: Critical - protocol insolvency through uncollateralised debt
- Fast validation: Run the baseline `collateral-remove` call, then the attacker-shaped one with the `ft` trait principal, and assert the attacker's net token balance change is zero or negative.
