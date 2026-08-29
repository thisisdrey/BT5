# Q3912: convert-to-scaled-debt via liquidate-multi: reach a state the guard immediately upstream of it never c

## Question
Does `liquidate-multi` (mainnet/contracts/market/v0-4-market.clar:1593) let an unprivileged attacker who controls which borrowers are placed early versus late in the batch reach `convert-to-scaled-debt` (mainnet/contracts/market/v0-4-market.clar:648) in a state where it reach a state the guard immediately upstream of it never contemplated? Given that it scales a token amount by the cached borrow index, rounding up on the borrow path, the invariant that only the acting principal's own position is mutated breaks and the result is permanent freezing of a position that can never be closed.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:648` -> `convert-to-scaled-debt`
- Entrypoint: `liquidate-multi` (`mainnet/contracts/market/v0-4-market.clar:1593`), unprivileged and publicly callable
- Attacker controls: which borrowers are placed early versus late in the batch
- Exploit idea: `convert-to-scaled-debt` scales a token amount by the cached borrow index, rounding up on the borrow path. Reach it through `liquidate-multi` and reach a state the guard immediately upstream of it never contemplated.
- Invariant to test: only the acting principal's own position is mutated
- Expected Immunefi impact: Critical - permanent freezing of a position that can never be closed
- Fast validation: Fuzz which borrowers are placed early versus late in the batch across its boundary values through `liquidate-multi` in simnet and assert `convert-to-scaled-debt` never returns a value that breaks the invariant.
