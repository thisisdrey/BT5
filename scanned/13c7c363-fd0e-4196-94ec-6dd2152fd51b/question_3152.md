# Q3152: socialize-debt-asset via liquidate-redeem: reach a state the guard immediately upstream of it never c

## Question
Does `liquidate-redeem` (mainnet/contracts/market/v0-4-market.clar:1604) let an unprivileged attacker who controls the vault whose share price the redemption moves reach `socialize-debt-asset` (mainnet/contracts/market/v0-4-market.clar:879) in a state where it reach a state the guard immediately upstream of it never contemplated? Given that it calls the vault write-down, then overwrites `index-cache` for the current timestamp mid-fold, and carries a `success` flag that short-circuits, the invariant that only the acting principal's own position is mutated breaks and the result is protocol insolvency.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:879` -> `socialize-debt-asset`
- Entrypoint: `liquidate-redeem` (`mainnet/contracts/market/v0-4-market.clar:1604`), unprivileged and publicly callable
- Attacker controls: the vault whose share price the redemption moves
- Exploit idea: `socialize-debt-asset` calls the vault write-down, then overwrites `index-cache` for the current timestamp mid-fold, and carries a `success` flag that short-circuits. Reach it through `liquidate-redeem` and reach a state the guard immediately upstream of it never contemplated.
- Invariant to test: only the acting principal's own position is mutated
- Expected Immunefi impact: Critical - protocol insolvency
- Fast validation: Write a Clarinet simnet test calling `liquidate-redeem` twice with the vault whose share price the redemption moves varied, and assert that the value `socialize-debt-asset` returns is identical in both runs; a divergence confirms the finding.
