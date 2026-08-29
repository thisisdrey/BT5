# Q3337: system-borrow via liquidate-multi: make a health check read a different position than the one

## Question
Can an unprivileged attacker entering through `liquidate-multi` (mainnet/contracts/market/v0-4-market.clar:1593), controlling the trait principals supplied per entry, drive `system-borrow` (mainnet/contracts/vault/v0-vault-stx.clar:865) — which independently computes `scaled-amount` with `mul-div-up` from its own `index`, duplicating the market's own scaling of the same borrow — to make a health check read a different position than the one that will exist, breaking the invariant that a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction, and cause direct theft of user funds at rest or in motion?

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:865` -> `system-borrow`
- Entrypoint: `liquidate-multi` (`mainnet/contracts/market/v0-4-market.clar:1593`), unprivileged and publicly callable
- Attacker controls: the trait principals supplied per entry
- Exploit idea: `system-borrow` independently computes `scaled-amount` with `mul-div-up` from its own `index`, duplicating the market's own scaling of the same borrow. Reach it through `liquidate-multi` and make a health check read a different position than the one that will exist.
- Invariant to test: a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction
- Expected Immunefi impact: Critical - direct theft of user funds at rest or in motion
- Fast validation: In `local-testing/tests` on a local fork, drive `liquidate-multi` with the trait principals supplied per entry, then read `system-borrow` state before and after in the same block and assert the two sides of the invariant are equal.
