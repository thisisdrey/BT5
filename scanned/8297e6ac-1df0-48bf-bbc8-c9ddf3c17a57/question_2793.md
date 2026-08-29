# Q2793: debt-remove-scaled via liquidate-redeem: convert a rounding direction into a repeatable extraction

## Question
Can an unprivileged attacker entering through `liquidate-redeem` (mainnet/contracts/market/v0-4-market.clar:1604), controlling the vault whose share price the redemption moves, drive `debt-remove-scaled` (mainnet/contracts/market/v0-market-vault.clar:473) — which clears the debt bit only when the remaining scaled debt is exactly zero — to convert a rounding direction into a repeatable extraction, breaking the invariant that conversions never round in the user's favour in either direction, and cause permanent freezing of funds?

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:473` -> `debt-remove-scaled`
- Entrypoint: `liquidate-redeem` (`mainnet/contracts/market/v0-4-market.clar:1604`), unprivileged and publicly callable
- Attacker controls: the vault whose share price the redemption moves
- Exploit idea: `debt-remove-scaled` clears the debt bit only when the remaining scaled debt is exactly zero. Reach it through `liquidate-redeem` and convert a rounding direction into a repeatable extraction.
- Invariant to test: conversions never round in the user's favour in either direction
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Snapshot every state variable `debt-remove-scaled` touches, run `liquidate-redeem` with the vault whose share price the redemption moves, recompute the invariant off-chain from the snapshot, and assert it matches the on-chain result.
