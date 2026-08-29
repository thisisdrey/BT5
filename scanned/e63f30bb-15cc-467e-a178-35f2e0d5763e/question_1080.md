# Q1080: resolve-or-create via liquidate-redeem: make an aggregate and its per-item breakdown disagree

## Question
Does `liquidate-redeem` (mainnet/contracts/market/v0-4-market.clar:1604) let an unprivileged attacker who controls the seized zToken amount that is immediately redeemed reach `resolve-or-create` (mainnet/contracts/market/v0-market-vault.clar:143) in a state where it make an aggregate and its per-item breakdown disagree? Given that it allocates a user id through `increment` for whatever principal the market names, the invariant that a value cached within a block still describes the state it was derived from breaks and the result is temporary freezing of funds.

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:143` -> `resolve-or-create`
- Entrypoint: `liquidate-redeem` (`mainnet/contracts/market/v0-4-market.clar:1604`), unprivileged and publicly callable
- Attacker controls: the seized zToken amount that is immediately redeemed
- Exploit idea: `resolve-or-create` allocates a user id through `increment` for whatever principal the market names. Reach it through `liquidate-redeem` and make an aggregate and its per-item breakdown disagree.
- Invariant to test: a value cached within a block still describes the state it was derived from
- Expected Immunefi impact: High - temporary freezing of funds
- Fast validation: Fuzz the seized zToken amount that is immediately redeemed across its boundary values through `liquidate-redeem` in simnet and assert `resolve-or-create` never returns a value that breaks the invariant.
