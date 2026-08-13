# Q2022: load_kamino_reserve: integration price adapter accepts mismatched reserve/market context [a-same-slot-pulse-followed] [adapter-mismatch]

## Question
Can an unprivileged attacker call `lending_pool_pulse_bank_price_cache` with a same-slot pulse followed by a core borrow or withdraw action so `load_kamino_reserve` loads or refreshes pricing from a mismatched reserve/market context, violating `external reserve or market state loaded for pricing must be exactly the bank-configured one and fresh enough for safe cache writes` and causing `High: exploitable cached misvaluation on a live integration bank`? Focus specifically on integration-backed price adapters where reserve/market and oracle contexts can be cross-wired.

## Target
- File/function: `programs/marginfi/src/state/price.rs` / `load_kamino_reserve`
- Entrypoint: `lending_pool_pulse_bank_price_cache`
- Attacker controls: a same-slot pulse followed by a core borrow or withdraw action
- Exploit idea: Probe helper functions that load external reserve/market state via remaining accounts and must bind them to the bank being mutated. Focus specifically on integration-backed price adapters where reserve/market and oracle contexts can be cross-wired.
- Invariant to test: external reserve or market state loaded for pricing must be exactly the bank-configured one and fresh enough for safe cache writes
- Expected Immunefi impact: High: exploitable cached misvaluation on a live integration bank
- Fast validation: Mix two similar external contexts and assert the pricing adapter cannot succeed unless the exact configured context is supplied. Provide two plausible adapter contexts and assert the price path rejects every mix-and-match combination.
