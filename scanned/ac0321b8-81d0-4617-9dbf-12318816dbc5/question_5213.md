# Q5213: calc-cumulative-debt via deposit: turn an accounting residue into a permanently unclosable p

## Question
Can an unprivileged attacker entering through `deposit` (mainnet/contracts/vault/v0-vault-stx.clar:763), controlling `recipient`, including a contract principal, drive `calc-cumulative-debt` (mainnet/contracts/vault/v0-vault-stx.clar:180) — which multiplies scaled principal by an index — to turn an accounting residue into a permanently unclosable position, breaking the invariant that a vault's underlying plus outstanding debt covers all shares and all supplier claims, and cause protocol insolvency?

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:180` -> `calc-cumulative-debt`
- Entrypoint: `deposit` (`mainnet/contracts/vault/v0-vault-stx.clar:763`), unprivileged and publicly callable
- Attacker controls: `recipient`, including a contract principal
- Exploit idea: `calc-cumulative-debt` multiplies scaled principal by an index. Reach it through `deposit` and turn an accounting residue into a permanently unclosable position.
- Invariant to test: a vault's underlying plus outstanding debt covers all shares and all supplier claims
- Expected Immunefi impact: Critical - protocol insolvency
- Fast validation: Run the baseline `deposit` call, then the attacker-shaped one with `recipient`, including a contract principal, and assert the attacker's net token balance change is zero or negative.
