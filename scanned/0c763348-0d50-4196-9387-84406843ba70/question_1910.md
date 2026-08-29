# Q1910: get-available-assets via redeem: make a health check read a different position than the one

## Question
Entering through `redeem` (mainnet/contracts/vault/v0-vault-stx.clar:797) while controlling `amount` of shares burned, can an unprivileged attacker make `get-available-assets` (mainnet/contracts/vault/v0-vault-stx.clar:481) make a health check read a different position than the one that will exist? `get-available-assets` reads real liquidity, a different quantity from the `assets` var that `redeem` also gates on, so the invariant that only the acting principal's own position is mutated would fail, yielding protocol insolvency.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:481` -> `get-available-assets`
- Entrypoint: `redeem` (`mainnet/contracts/vault/v0-vault-stx.clar:797`), unprivileged and publicly callable
- Attacker controls: `amount` of shares burned
- Exploit idea: `get-available-assets` reads real liquidity, a different quantity from the `assets` var that `redeem` also gates on. Reach it through `redeem` and make a health check read a different position than the one that will exist.
- Invariant to test: only the acting principal's own position is mutated
- Expected Immunefi impact: Critical - protocol insolvency
- Fast validation: Write a Clarinet simnet test calling `redeem` twice with `amount` of shares burned varied, and assert that the value `get-available-assets` returns is identical in both runs; a divergence confirms the finding.
