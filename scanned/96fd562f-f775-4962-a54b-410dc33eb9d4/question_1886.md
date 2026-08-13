# Q1886: check_primary_oracle_key: price-type routing picks a safer-looking but wrong value [a-bank-using-a-derivative] [adapter-mismatch]

## Question
Can an unprivileged attacker use `lending_pool_pulse_bank_price_cache` with a bank using a derivative or integration-backed price adapter so `check_primary_oracle_key` routes to the wrong price type for the mutation being performed, violating `permissionless cache refresh must only ever bind the bank to its exact configured primary price source` and leading to `High: exploitable cached mispricing that later enables theft or insolvency`? Focus specifically on integration-backed price adapters where reserve/market and oracle contexts can be cross-wired.

## Target
- File/function: `programs/marginfi/src/state/price.rs` / `check_primary_oracle_key`
- Entrypoint: `lending_pool_pulse_bank_price_cache`
- Attacker controls: a bank using a derivative or integration-backed price adapter
- Exploit idea: Audit distinctions like spot vs TWAP vs cache vs confidence-ignored pricing to ensure each value-moving path uses the intended conservative source. Focus specifically on integration-backed price adapters where reserve/market and oracle contexts can be cross-wired.
- Invariant to test: permissionless cache refresh must only ever bind the bank to its exact configured primary price source
- Expected Immunefi impact: High: exploitable cached mispricing that later enables theft or insolvency
- Fast validation: Exercise paths where price-type choice matters economically and assert the selected type matches protocol design for that exact action. Provide two plausible adapter contexts and assert the price path rejects every mix-and-match combination.
