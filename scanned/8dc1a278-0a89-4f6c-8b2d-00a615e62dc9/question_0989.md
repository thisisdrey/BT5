# Q0989: interest-rate via liquidate-redeem: reach a state the guard immediately upstream of it never c

## Question
Can an unprivileged attacker entering through `liquidate-redeem` (mainnet/contracts/market/v0-4-market.clar:1604), controlling the redemption receiver, drive `interest-rate` (mainnet/contracts/vault/v0-vault-stx.clar:371) — which interpolates the packed curve at the current utilization — to reach a state the guard immediately upstream of it never contemplated, breaking the invariant that no position row exists that the position mask does not represent, and cause permanent freezing of unclaimed yield?

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:371` -> `interest-rate`
- Entrypoint: `liquidate-redeem` (`mainnet/contracts/market/v0-4-market.clar:1604`), unprivileged and publicly callable
- Attacker controls: the redemption receiver
- Exploit idea: `interest-rate` interpolates the packed curve at the current utilization. Reach it through `liquidate-redeem` and reach a state the guard immediately upstream of it never contemplated.
- Invariant to test: no position row exists that the position mask does not represent
- Expected Immunefi impact: High - permanent freezing of unclaimed yield
- Fast validation: Run the baseline `liquidate-redeem` call, then the attacker-shaped one with the redemption receiver, and assert the attacker's net token balance change is zero or negative.
