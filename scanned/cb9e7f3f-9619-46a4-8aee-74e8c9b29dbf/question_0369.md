# Q0369: accrue-user-collateral via liquidate-redeem: make two code sites that must agree disagree by an attacke

## Question
Can an unprivileged attacker entering through `liquidate-redeem` (mainnet/contracts/market/v0-4-market.clar:1604), controlling the seized zToken amount that is immediately redeemed, drive `accrue-user-collateral` (mainnet/contracts/market/v0-4-market.clar:270) — which accrues only rows that `is-ztoken` recognises, skipping everything else — to make two code sites that must agree disagree by an attacker-chosen amount, breaking the invariant that only the acting principal's own position is mutated, and cause temporary freezing of funds?

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:270` -> `accrue-user-collateral`
- Entrypoint: `liquidate-redeem` (`mainnet/contracts/market/v0-4-market.clar:1604`), unprivileged and publicly callable
- Attacker controls: the seized zToken amount that is immediately redeemed
- Exploit idea: `accrue-user-collateral` accrues only rows that `is-ztoken` recognises, skipping everything else. Reach it through `liquidate-redeem` and make two code sites that must agree disagree by an attacker-chosen amount.
- Invariant to test: only the acting principal's own position is mutated
- Expected Immunefi impact: High - temporary freezing of funds
- Fast validation: Snapshot every state variable `accrue-user-collateral` touches, run `liquidate-redeem` with the seized zToken amount that is immediately redeemed, recompute the invariant off-chain from the snapshot, and assert it matches the on-chain result.
