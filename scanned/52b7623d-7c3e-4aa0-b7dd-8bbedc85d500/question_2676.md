# Q2676: price-multi-resolve via liquidate-redeem: make two code sites that must agree disagree by an attacke

## Question
Does `liquidate-redeem` (mainnet/contracts/market/v0-4-market.clar:1604) let an unprivileged attacker who controls the redemption receiver reach `price-multi-resolve` (mainnet/contracts/market/v0-4-market.clar:397) in a state where it make two code sites that must agree disagree by an attacker-chosen amount? Given that it folds `iter-price-multi` into a POSITIONAL price list, asserting only the `valid` flag at the end, the invariant that a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction breaks and the result is permanent freezing of funds.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:397` -> `price-multi-resolve`
- Entrypoint: `liquidate-redeem` (`mainnet/contracts/market/v0-4-market.clar:1604`), unprivileged and publicly callable
- Attacker controls: the redemption receiver
- Exploit idea: `price-multi-resolve` folds `iter-price-multi` into a POSITIONAL price list, asserting only the `valid` flag at the end. Reach it through `liquidate-redeem` and make two code sites that must agree disagree by an attacker-chosen amount.
- Invariant to test: a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Fuzz the redemption receiver across its boundary values through `liquidate-redeem` in simnet and assert `price-multi-resolve` never returns a value that breaks the invariant.
