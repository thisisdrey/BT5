# Q0822: increment via liquidate-redeem: convert a rounding direction into a repeatable extraction

## Question
Entering through `liquidate-redeem` (mainnet/contracts/market/v0-4-market.clar:1604) while controlling the redemption receiver, can an unprivileged attacker make `increment` (mainnet/contracts/market/v0-market-vault.clar:137) convert a rounding direction into a repeatable extraction? `increment` advances the user-id nonce, so the invariant that every asset a position holds enters the health evaluation exactly once would fail, yielding permanent freezing of funds.

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:137` -> `increment`
- Entrypoint: `liquidate-redeem` (`mainnet/contracts/market/v0-4-market.clar:1604`), unprivileged and publicly callable
- Attacker controls: the redemption receiver
- Exploit idea: `increment` advances the user-id nonce. Reach it through `liquidate-redeem` and convert a rounding direction into a repeatable extraction.
- Invariant to test: every asset a position holds enters the health evaluation exactly once
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Fuzz the redemption receiver across its boundary values through `liquidate-redeem` in simnet and assert `increment` never returns a value that breaks the invariant.
