# Q1238: socialize-debt via liquidate-multi: make a health check read a different position than the one

## Question
Entering through `liquidate-multi` (mainnet/contracts/market/v0-4-market.clar:1593) while controlling which borrowers are placed early versus late in the batch, can an unprivileged attacker make `socialize-debt` (mainnet/contracts/vault/v0-vault-stx.clar:944) make a health check read a different position than the one that will exist? `socialize-debt` writes down `lindex` by one ratio while reducing `assets` by a completely different `principal-reduction`, so the invariant that only the acting principal's own position is mutated would fail, yielding protocol insolvency through uncollateralised debt.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:944` -> `socialize-debt`
- Entrypoint: `liquidate-multi` (`mainnet/contracts/market/v0-4-market.clar:1593`), unprivileged and publicly callable
- Attacker controls: which borrowers are placed early versus late in the batch
- Exploit idea: `socialize-debt` writes down `lindex` by one ratio while reducing `assets` by a completely different `principal-reduction`. Reach it through `liquidate-multi` and make a health check read a different position than the one that will exist.
- Invariant to test: only the acting principal's own position is mutated
- Expected Immunefi impact: Critical - protocol insolvency through uncollateralised debt
- Fast validation: Write a Clarinet simnet test calling `liquidate-multi` twice with which borrowers are placed early versus late in the batch varied, and assert that the value `socialize-debt` returns is identical in both runs; a divergence confirms the finding.
