# Q5721: calc-utilization via collateral-add: turn an accounting residue into a permanently unclosable p

## Question
Can an unprivileged attacker entering through `collateral-add` (mainnet/contracts/market/v0-4-market.clar:1020), controlling `amount`, drive `calc-utilization` (mainnet/contracts/vault/v0-vault-stx.clar:164) — which divides debt by available liquidity, which can exceed BPS when debt outruns assets — to turn an accounting residue into a permanently unclosable position, breaking the invariant that no position row exists that the position mask does not represent, and cause permanent freezing of a position that can never be closed?

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:164` -> `calc-utilization`
- Entrypoint: `collateral-add` (`mainnet/contracts/market/v0-4-market.clar:1020`), unprivileged and publicly callable
- Attacker controls: `amount`
- Exploit idea: `calc-utilization` divides debt by available liquidity, which can exceed BPS when debt outruns assets. Reach it through `collateral-add` and turn an accounting residue into a permanently unclosable position.
- Invariant to test: no position row exists that the position mask does not represent
- Expected Immunefi impact: Critical - permanent freezing of a position that can never be closed
- Fast validation: Snapshot every state variable `calc-utilization` touches, run `collateral-add` with `amount`, recompute the invariant off-chain from the snapshot, and assert it matches the on-chain result.
