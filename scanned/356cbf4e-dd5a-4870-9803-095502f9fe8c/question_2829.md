# Q2829: receive-tokens via repay: convert a rounding direction into a repeatable extraction

## Question
Can an unprivileged attacker entering through `repay` (mainnet/contracts/market/v0-4-market.clar:1316), controlling the `ft` trait principal, drive `receive-tokens` (mainnet/contracts/market/v0-market-vault.clar:256) — which pulls an asset from a named account — to convert a rounding direction into a repeatable extraction, breaking the invariant that a value cached within a block still describes the state it was derived from, and cause permanent freezing of funds?

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:256` -> `receive-tokens`
- Entrypoint: `repay` (`mainnet/contracts/market/v0-4-market.clar:1316`), unprivileged and publicly callable
- Attacker controls: the `ft` trait principal
- Exploit idea: `receive-tokens` pulls an asset from a named account. Reach it through `repay` and convert a rounding direction into a repeatable extraction.
- Invariant to test: a value cached within a block still describes the state it was derived from
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Snapshot every state variable `receive-tokens` touches, run `repay` with the `ft` trait principal, recompute the invariant off-chain from the snapshot, and assert it matches the on-chain result.
