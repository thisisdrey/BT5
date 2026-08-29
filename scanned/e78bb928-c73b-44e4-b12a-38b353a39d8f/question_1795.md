# Q1795: vault-system-repay via liquidate-multi: turn an accounting residue into a permanently unclosable p

## Question
`vault-system-repay` (mainnet/contracts/market/v0-4-market.clar:207) routes a repayment to one of six vaults by asset id. Can an unprivileged caller of `liquidate-multi` (mainnet/contracts/market/v0-4-market.clar:1593), by choosing the trait principals supplied per entry, use that to turn an accounting residue into a permanently unclosable position, violating the invariant that every asset a position holds enters the health evaluation exactly once and producing direct theft of another user's collateral?

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:207` -> `vault-system-repay`
- Entrypoint: `liquidate-multi` (`mainnet/contracts/market/v0-4-market.clar:1593`), unprivileged and publicly callable
- Attacker controls: the trait principals supplied per entry
- Exploit idea: `vault-system-repay` routes a repayment to one of six vaults by asset id. Reach it through `liquidate-multi` and turn an accounting residue into a permanently unclosable position.
- Invariant to test: every asset a position holds enters the health evaluation exactly once
- Expected Immunefi impact: Critical - direct theft of another user's collateral
- Fast validation: In `local-testing/tests` on a local fork, drive `liquidate-multi` with the trait principals supplied per entry, then read `vault-system-repay` state before and after in the same block and assert the two sides of the invariant are equal.
