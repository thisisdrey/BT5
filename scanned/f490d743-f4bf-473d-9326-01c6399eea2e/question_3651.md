# Q3651: calc-utilization via supply-collateral-add: make an aggregate and its per-item breakdown disagree

## Question
`calc-utilization` (mainnet/contracts/vault/v0-vault-stx.clar:164) divides debt by available liquidity, which can exceed BPS when debt outruns assets. Can an unprivileged caller of `supply-collateral-add` (mainnet/contracts/market/v0-4-market.clar:1175), by choosing vault share price at the moment of the deposit leg, use that to make an aggregate and its per-item breakdown disagree, violating the invariant that a vault's underlying plus outstanding debt covers all shares and all supplier claims and producing permanent freezing of a position that can never be closed?

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:164` -> `calc-utilization`
- Entrypoint: `supply-collateral-add` (`mainnet/contracts/market/v0-4-market.clar:1175`), unprivileged and publicly callable
- Attacker controls: vault share price at the moment of the deposit leg
- Exploit idea: `calc-utilization` divides debt by available liquidity, which can exceed BPS when debt outruns assets. Reach it through `supply-collateral-add` and make an aggregate and its per-item breakdown disagree.
- Invariant to test: a vault's underlying plus outstanding debt covers all shares and all supplier claims
- Expected Immunefi impact: Critical - permanent freezing of a position that can never be closed
- Fast validation: Snapshot every state variable `calc-utilization` touches, run `supply-collateral-add` with vault share price at the moment of the deposit leg, recompute the invariant off-chain from the snapshot, and assert it matches the on-chain result.
