# Q0661: debt-add-scaled via supply-collateral-add: make two code sites that must agree disagree by an attacke

## Question
Can an unprivileged attacker entering through `supply-collateral-add` (mainnet/contracts/market/v0-4-market.clar:1175), controlling `amount`, drive `debt-add-scaled` (mainnet/contracts/market/v0-market-vault.clar:442) — which stamps `last-borrow-block` from `stacks-block-height` onto the named ACCOUNT, not the caller — to make two code sites that must agree disagree by an attacker-chosen amount, breaking the invariant that collateral seized equals debt repaid scaled by the penalty, and only above the liquidation LTV, and cause direct theft of another user's collateral?

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:442` -> `debt-add-scaled`
- Entrypoint: `supply-collateral-add` (`mainnet/contracts/market/v0-4-market.clar:1175`), unprivileged and publicly callable
- Attacker controls: `amount`
- Exploit idea: `debt-add-scaled` stamps `last-borrow-block` from `stacks-block-height` onto the named ACCOUNT, not the caller. Reach it through `supply-collateral-add` and make two code sites that must agree disagree by an attacker-chosen amount.
- Invariant to test: collateral seized equals debt repaid scaled by the penalty, and only above the liquidation LTV
- Expected Immunefi impact: Critical - direct theft of another user's collateral
- Fast validation: In `local-testing/tests` on a local fork, drive `supply-collateral-add` with `amount`, then read `debt-add-scaled` state before and after in the same block and assert the two sides of the invariant are equal.
