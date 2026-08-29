# Q0807: add-user-collateral via liquidate-multi: compose two individually correct mechanisms into an incorr

## Question
`add-user-collateral` (mainnet/contracts/market/v0-market-vault.clar:198) adds to the collateral row with a graceful u0 default. Can an unprivileged caller of `liquidate-multi` (mainnet/contracts/market/v0-4-market.clar:1593), by choosing which borrowers are placed early versus late in the batch, use that to compose two individually correct mechanisms into an incorrect result, violating the invariant that conversions never round in the user's favour in either direction and producing permanent freezing of a position that can never be closed?

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:198` -> `add-user-collateral`
- Entrypoint: `liquidate-multi` (`mainnet/contracts/market/v0-4-market.clar:1593`), unprivileged and publicly callable
- Attacker controls: which borrowers are placed early versus late in the batch
- Exploit idea: `add-user-collateral` adds to the collateral row with a graceful u0 default. Reach it through `liquidate-multi` and compose two individually correct mechanisms into an incorrect result.
- Invariant to test: conversions never round in the user's favour in either direction
- Expected Immunefi impact: Critical - permanent freezing of a position that can never be closed
- Fast validation: Snapshot every state variable `add-user-collateral` touches, run `liquidate-multi` with which borrowers are placed early versus late in the batch, recompute the invariant off-chain from the snapshot, and assert it matches the on-chain result.
