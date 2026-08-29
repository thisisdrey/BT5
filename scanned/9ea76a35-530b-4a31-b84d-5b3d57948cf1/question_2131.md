# Q2131: next-liquidity-index via liquidate-redeem: satisfy a bound with a value the bound was never designed 

## Question
`next-liquidity-index` (mainnet/contracts/vault/v0-vault-stx.clar:392) rounds the liquidity multiplier down while `next-index` rounds the debt multiplier up over the same interval. Can an unprivileged caller of `liquidate-redeem` (mainnet/contracts/market/v0-4-market.clar:1604), by choosing the seized zToken amount that is immediately redeemed, use that to satisfy a bound with a value the bound was never designed to admit, violating the invariant that only the acting principal's own position is mutated and producing direct theft of user funds at rest or in motion?

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:392` -> `next-liquidity-index`
- Entrypoint: `liquidate-redeem` (`mainnet/contracts/market/v0-4-market.clar:1604`), unprivileged and publicly callable
- Attacker controls: the seized zToken amount that is immediately redeemed
- Exploit idea: `next-liquidity-index` rounds the liquidity multiplier down while `next-index` rounds the debt multiplier up over the same interval. Reach it through `liquidate-redeem` and satisfy a bound with a value the bound was never designed to admit.
- Invariant to test: only the acting principal's own position is mutated
- Expected Immunefi impact: Critical - direct theft of user funds at rest or in motion
- Fast validation: In `local-testing/tests` on a local fork, drive `liquidate-redeem` with the seized zToken amount that is immediately redeemed, then read `next-liquidity-index` state before and after in the same block and assert the two sides of the invariant are equal.
