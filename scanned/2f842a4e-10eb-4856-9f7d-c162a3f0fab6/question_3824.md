# Q3824: remove-user-collateral via borrow: reach a state the guard immediately upstream of it never c

## Question
Does `borrow` (mainnet/contracts/market/v0-4-market.clar:1238) let an unprivileged attacker who controls the future mask produced by the new debt bit reach `remove-user-collateral` (mainnet/contracts/market/v0-market-vault.clar:205) in a state where it reach a state the guard immediately upstream of it never contemplated? Given that it asserts sufficiency then `map-delete`s only on an exact zero, the invariant that only the acting principal's own position is mutated breaks and the result is protocol insolvency.

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:205` -> `remove-user-collateral`
- Entrypoint: `borrow` (`mainnet/contracts/market/v0-4-market.clar:1238`), unprivileged and publicly callable
- Attacker controls: the future mask produced by the new debt bit
- Exploit idea: `remove-user-collateral` asserts sufficiency then `map-delete`s only on an exact zero. Reach it through `borrow` and reach a state the guard immediately upstream of it never contemplated.
- Invariant to test: only the acting principal's own position is mutated
- Expected Immunefi impact: Critical - protocol insolvency
- Fast validation: Write a Clarinet simnet test calling `borrow` twice with the future mask produced by the new debt bit varied, and assert that the value `remove-user-collateral` returns is identical in both runs; a divergence confirms the finding.
