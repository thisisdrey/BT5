# Q4187: get-position via liquidate-multi: make two code sites that must agree disagree by an attacke

## Question
`get-position` (mainnet/contracts/market/v0-4-market.clar:466) returns only rows whose bit is set in the ENABLED bitmap. Can an unprivileged caller of `liquidate-multi` (mainnet/contracts/market/v0-4-market.clar:1593), by choosing which borrowers are placed early versus late in the batch, use that to make two code sites that must agree disagree by an attacker-chosen amount, violating the invariant that conversions never round in the user's favour in either direction and producing protocol insolvency?

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:466` -> `get-position`
- Entrypoint: `liquidate-multi` (`mainnet/contracts/market/v0-4-market.clar:1593`), unprivileged and publicly callable
- Attacker controls: which borrowers are placed early versus late in the batch
- Exploit idea: `get-position` returns only rows whose bit is set in the ENABLED bitmap. Reach it through `liquidate-multi` and make two code sites that must agree disagree by an attacker-chosen amount.
- Invariant to test: conversions never round in the user's favour in either direction
- Expected Immunefi impact: Critical - protocol insolvency
- Fast validation: Run the baseline `liquidate-multi` call, then the attacker-shaped one with which borrowers are placed early versus late in the batch, and assert the attacker's net token balance change is zero or negative.
