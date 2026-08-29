# Q4367: get-cached-indexes via liquidate-redeem: make two code sites that must agree disagree by an attacke

## Question
`get-cached-indexes` (mainnet/contracts/market/v0-4-market.clar:944) reads the per-block cache entry that `resolve-ztoken` and the debt conversions depend on. Can an unprivileged caller of `liquidate-redeem` (mainnet/contracts/market/v0-4-market.clar:1604), by choosing the vault whose share price the redemption moves, use that to make two code sites that must agree disagree by an attacker-chosen amount, violating the invariant that a value cached within a block still describes the state it was derived from and producing protocol insolvency?

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:944` -> `get-cached-indexes`
- Entrypoint: `liquidate-redeem` (`mainnet/contracts/market/v0-4-market.clar:1604`), unprivileged and publicly callable
- Attacker controls: the vault whose share price the redemption moves
- Exploit idea: `get-cached-indexes` reads the per-block cache entry that `resolve-ztoken` and the debt conversions depend on. Reach it through `liquidate-redeem` and make two code sites that must agree disagree by an attacker-chosen amount.
- Invariant to test: a value cached within a block still describes the state it was derived from
- Expected Immunefi impact: Critical - protocol insolvency
- Fast validation: Run the baseline `liquidate-redeem` call, then the attacker-shaped one with the vault whose share price the redemption moves, and assert the attacker's net token balance change is zero or negative.
