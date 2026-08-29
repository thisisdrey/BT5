# Q2773: collateral-add via liquidate-multi: convert a rounding direction into a repeatable extraction

## Question
Can an unprivileged attacker entering through `liquidate-multi` (mainnet/contracts/market/v0-4-market.clar:1593), controlling which borrowers are placed early versus late in the batch, drive `collateral-add` (mainnet/contracts/market/v0-market-vault.clar:374) — which evaluates the map write and `mask-update` as `let` bindings BEFORE `check-impl-auth`, the pause state and the amount assertion — to convert a rounding direction into a repeatable extraction, breaking the invariant that a value cached within a block still describes the state it was derived from, and cause direct theft of user funds at rest or in motion?

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:374` -> `collateral-add`
- Entrypoint: `liquidate-multi` (`mainnet/contracts/market/v0-4-market.clar:1593`), unprivileged and publicly callable
- Attacker controls: which borrowers are placed early versus late in the batch
- Exploit idea: `collateral-add` evaluates the map write and `mask-update` as `let` bindings BEFORE `check-impl-auth`, the pause state and the amount assertion. Reach it through `liquidate-multi` and convert a rounding direction into a repeatable extraction.
- Invariant to test: a value cached within a block still describes the state it was derived from
- Expected Immunefi impact: Critical - direct theft of user funds at rest or in motion
- Fast validation: In `local-testing/tests` on a local fork, drive `liquidate-multi` with which borrowers are placed early versus late in the batch, then read `collateral-add` state before and after in the same block and assert the two sides of the invariant are equal.
