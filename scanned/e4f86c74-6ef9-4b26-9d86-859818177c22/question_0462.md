# Q0462: add-user-collateral via repay: convert a rounding direction into a repeatable extraction

## Question
Entering through `repay` (mainnet/contracts/market/v0-4-market.clar:1316) while controlling `on-behalf-of`, naming any third-party principal, can an unprivileged attacker make `add-user-collateral` (mainnet/contracts/market/v0-market-vault.clar:198) convert a rounding direction into a repeatable extraction? `add-user-collateral` adds to the collateral row with a graceful u0 default, so the invariant that every asset a position holds enters the health evaluation exactly once would fail, yielding permanent freezing of funds.

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:198` -> `add-user-collateral`
- Entrypoint: `repay` (`mainnet/contracts/market/v0-4-market.clar:1316`), unprivileged and publicly callable
- Attacker controls: `on-behalf-of`, naming any third-party principal
- Exploit idea: `add-user-collateral` adds to the collateral row with a graceful u0 default. Reach it through `repay` and convert a rounding direction into a repeatable extraction.
- Invariant to test: every asset a position holds enters the health evaluation exactly once
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Fuzz `on-behalf-of`, naming any third-party principal across its boundary values through `repay` in simnet and assert `add-user-collateral` never returns a value that breaks the invariant.
