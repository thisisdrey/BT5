# Q3240: calc-utilization via liquidate-multi: reach a state the guard immediately upstream of it never c

## Question
Does `liquidate-multi` (mainnet/contracts/market/v0-4-market.clar:1593) let an unprivileged attacker who controls which borrowers are placed early versus late in the batch reach `calc-utilization` (mainnet/contracts/vault/v0-vault-stx.clar:164) in a state where it reach a state the guard immediately upstream of it never contemplated? Given that it divides debt by available liquidity, which can exceed BPS when debt outruns assets, the invariant that only the acting principal's own position is mutated breaks and the result is temporary freezing of funds.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:164` -> `calc-utilization`
- Entrypoint: `liquidate-multi` (`mainnet/contracts/market/v0-4-market.clar:1593`), unprivileged and publicly callable
- Attacker controls: which borrowers are placed early versus late in the batch
- Exploit idea: `calc-utilization` divides debt by available liquidity, which can exceed BPS when debt outruns assets. Reach it through `liquidate-multi` and reach a state the guard immediately upstream of it never contemplated.
- Invariant to test: only the acting principal's own position is mutated
- Expected Immunefi impact: High - temporary freezing of funds
- Fast validation: Fuzz which borrowers are placed early versus late in the batch across its boundary values through `liquidate-multi` in simnet and assert `calc-utilization` never returns a value that breaks the invariant.
