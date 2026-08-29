# Q1382: lookup via borrow: make a health check read a different position than the one

## Question
Entering through `borrow` (mainnet/contracts/market/v0-4-market.clar:1238) while controlling the `ft` trait principal, can an unprivileged attacker make `lookup` (mainnet/contracts/registry/v0-assets.clar:139) make a health check read a different position than the one that will exist? `lookup` returns the registry record, including the `decimals` captured once at registration, so the invariant that only the acting principal's own position is mutated would fail, yielding protocol insolvency through uncollateralised debt.

## Target
- File/function: `mainnet/contracts/registry/v0-assets.clar:139` -> `lookup`
- Entrypoint: `borrow` (`mainnet/contracts/market/v0-4-market.clar:1238`), unprivileged and publicly callable
- Attacker controls: the `ft` trait principal
- Exploit idea: `lookup` returns the registry record, including the `decimals` captured once at registration. Reach it through `borrow` and make a health check read a different position than the one that will exist.
- Invariant to test: only the acting principal's own position is mutated
- Expected Immunefi impact: Critical - protocol insolvency through uncollateralised debt
- Fast validation: Write a Clarinet simnet test calling `borrow` twice with the `ft` trait principal varied, and assert that the value `lookup` returns is identical in both runs; a divergence confirms the finding.
