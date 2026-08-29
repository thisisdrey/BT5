# Q5327: filter-u128 via collateral-remove: reach a state the guard immediately upstream of it never c

## Question
`filter-u128` (mainnet/contracts/registry/v0-egroup.clar:97) filters a 128-entry bucket list. Can an unprivileged caller of `collateral-remove` (mainnet/contracts/market/v0-4-market.clar:1107), by choosing whether the position has any enabled debt row (the has-debt branch), use that to reach a state the guard immediately upstream of it never contemplated, violating the invariant that every asset a position holds enters the health evaluation exactly once and producing protocol insolvency through uncollateralised debt?

## Target
- File/function: `mainnet/contracts/registry/v0-egroup.clar:97` -> `filter-u128`
- Entrypoint: `collateral-remove` (`mainnet/contracts/market/v0-4-market.clar:1107`), unprivileged and publicly callable
- Attacker controls: whether the position has any enabled debt row (the has-debt branch)
- Exploit idea: `filter-u128` filters a 128-entry bucket list. Reach it through `collateral-remove` and reach a state the guard immediately upstream of it never contemplated.
- Invariant to test: every asset a position holds enters the health evaluation exactly once
- Expected Immunefi impact: Critical - protocol insolvency through uncollateralised debt
- Fast validation: Run the baseline `collateral-remove` call, then the attacker-shaped one with whether the position has any enabled debt row (the has-debt branch), and assert the attacker's net token balance change is zero or negative.
