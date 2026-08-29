# Q5321: debt-remove-scaled via repay: turn an accounting residue into a permanently unclosable p

## Question
Can an unprivileged attacker entering through `repay` (mainnet/contracts/market/v0-4-market.clar:1316), controlling `on-behalf-of`, naming any third-party principal, drive `debt-remove-scaled` (mainnet/contracts/market/v0-market-vault.clar:473) — which clears the debt bit only when the remaining scaled debt is exactly zero — to turn an accounting residue into a permanently unclosable position, breaking the invariant that no position row exists that the position mask does not represent, and cause protocol insolvency?

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:473` -> `debt-remove-scaled`
- Entrypoint: `repay` (`mainnet/contracts/market/v0-4-market.clar:1316`), unprivileged and publicly callable
- Attacker controls: `on-behalf-of`, naming any third-party principal
- Exploit idea: `debt-remove-scaled` clears the debt bit only when the remaining scaled debt is exactly zero. Reach it through `repay` and turn an accounting residue into a permanently unclosable position.
- Invariant to test: no position row exists that the position mask does not represent
- Expected Immunefi impact: Critical - protocol insolvency
- Fast validation: Run the baseline `repay` call, then the attacker-shaped one with `on-behalf-of`, naming any third-party principal, and assert the attacker's net token balance change is zero or negative.
