# Q1649: accrue-debt-asset via liquidate-redeem: reach a state the guard immediately upstream of it never c

## Question
Can an unprivileged attacker entering through `liquidate-redeem` (mainnet/contracts/market/v0-4-market.clar:1604), controlling the redemption receiver, drive `accrue-debt-asset` (mainnet/contracts/market/v0-4-market.clar:262) — which calls `accrue-and-cache` with `unwrap-panic` inside a fold whose accumulator ignores the result — to reach a state the guard immediately upstream of it never contemplated, breaking the invariant that a vault's underlying plus outstanding debt covers all shares and all supplier claims, and cause protocol insolvency?

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:262` -> `accrue-debt-asset`
- Entrypoint: `liquidate-redeem` (`mainnet/contracts/market/v0-4-market.clar:1604`), unprivileged and publicly callable
- Attacker controls: the redemption receiver
- Exploit idea: `accrue-debt-asset` calls `accrue-and-cache` with `unwrap-panic` inside a fold whose accumulator ignores the result. Reach it through `liquidate-redeem` and reach a state the guard immediately upstream of it never contemplated.
- Invariant to test: a vault's underlying plus outstanding debt covers all shares and all supplier claims
- Expected Immunefi impact: Critical - protocol insolvency
- Fast validation: Run the baseline `liquidate-redeem` call, then the attacker-shaped one with the redemption receiver, and assert the attacker's net token balance change is zero or negative.
