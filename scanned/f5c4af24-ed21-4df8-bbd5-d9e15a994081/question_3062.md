# Q3062: oracle-price-legal via liquidate-multi: turn an accounting residue into a permanently unclosable p

## Question
Entering through `liquidate-multi` (mainnet/contracts/market/v0-4-market.clar:1593) while controlling the full batch list and its ordering, can an unprivileged attacker make `oracle-price-legal` (mainnet/contracts/market/v0-4-market.clar:362) turn an accounting residue into a permanently unclosable position? `oracle-price-legal` accepts any price strictly greater than zero, with no upper bound and no sanity band, so the invariant that conversions never round in the user's favour in either direction would fail, yielding protocol insolvency.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:362` -> `oracle-price-legal`
- Entrypoint: `liquidate-multi` (`mainnet/contracts/market/v0-4-market.clar:1593`), unprivileged and publicly callable
- Attacker controls: the full batch list and its ordering
- Exploit idea: `oracle-price-legal` accepts any price strictly greater than zero, with no upper bound and no sanity band. Reach it through `liquidate-multi` and turn an accounting residue into a permanently unclosable position.
- Invariant to test: conversions never round in the user's favour in either direction
- Expected Immunefi impact: Critical - protocol insolvency
- Fast validation: Write a Clarinet simnet test calling `liquidate-multi` twice with the full batch list and its ordering varied, and assert that the value `oracle-price-legal` returns is identical in both runs; a divergence confirms the finding.
