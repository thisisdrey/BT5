# Q4311: iter-lookup-collateral via borrow: make two code sites that must agree disagree by an attacke

## Question
`iter-lookup-collateral` (mainnet/contracts/market/v0-market-vault.clar:180) skips rows failing `relevant`, so a disabled asset's collateral vanishes from the returned position. Can an unprivileged caller of `borrow` (mainnet/contracts/market/v0-4-market.clar:1238), by choosing `amount`, use that to make two code sites that must agree disagree by an attacker-chosen amount, violating the invariant that a value cached within a block still describes the state it was derived from and producing permanent freezing of funds?

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:180` -> `iter-lookup-collateral`
- Entrypoint: `borrow` (`mainnet/contracts/market/v0-4-market.clar:1238`), unprivileged and publicly callable
- Attacker controls: `amount`
- Exploit idea: `iter-lookup-collateral` skips rows failing `relevant`, so a disabled asset's collateral vanishes from the returned position. Reach it through `borrow` and make two code sites that must agree disagree by an attacker-chosen amount.
- Invariant to test: a value cached within a block still describes the state it was derived from
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Snapshot every state variable `iter-lookup-collateral` touches, run `borrow` with `amount`, recompute the invariant off-chain from the snapshot, and assert it matches the on-chain result.
