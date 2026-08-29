# Q2700: resolve-dia via liquidate-redeem: make two code sites that must agree disagree by an attacke

## Question
Does `liquidate-redeem` (mainnet/contracts/market/v0-4-market.clar:1604) let an unprivileged attacker who controls the redemption receiver reach `resolve-dia` (mainnet/contracts/market/v0-4-market.clar:326) in a state where it make two code sites that must agree disagree by an attacker-chosen amount? Given that it derives a (string-ascii 32) key from a (buff 32) ident, the invariant that a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction breaks and the result is temporary freezing of funds.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:326` -> `resolve-dia`
- Entrypoint: `liquidate-redeem` (`mainnet/contracts/market/v0-4-market.clar:1604`), unprivileged and publicly callable
- Attacker controls: the redemption receiver
- Exploit idea: `resolve-dia` derives a (string-ascii 32) key from a (buff 32) ident. Reach it through `liquidate-redeem` and make two code sites that must agree disagree by an attacker-chosen amount.
- Invariant to test: a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction
- Expected Immunefi impact: High - temporary freezing of funds
- Fast validation: Fuzz the redemption receiver across its boundary values through `liquidate-redeem` in simnet and assert `resolve-dia` never returns a value that breaks the invariant.
