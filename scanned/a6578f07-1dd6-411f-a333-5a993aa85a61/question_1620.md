# Q1620: get_price_of_type: price-type routing picks a safer-looking but wrong value [a-bank-whose-cached-and] [adapter-mismatch]

## Question
Can an unprivileged attacker use `lending_account_borrow` with a bank whose cached and live price context diverge at the borrow threshold so `get_price_of_type` routes to the wrong price type for the mutation being performed, violating `the priced value used to admit borrowing must come from the exact configured price source and intended conservative price type` and leading to `Critical: unbacked borrowing or solvency loss from misvaluation`? Focus specifically on integration-backed price adapters where reserve/market and oracle contexts can be cross-wired.

## Target
- File/function: `programs/marginfi/src/state/price.rs` / `get_price_of_type`
- Entrypoint: `lending_account_borrow`
- Attacker controls: a bank whose cached and live price context diverge at the borrow threshold
- Exploit idea: Audit distinctions like spot vs TWAP vs cache vs confidence-ignored pricing to ensure each value-moving path uses the intended conservative source. Focus specifically on integration-backed price adapters where reserve/market and oracle contexts can be cross-wired.
- Invariant to test: the priced value used to admit borrowing must come from the exact configured price source and intended conservative price type
- Expected Immunefi impact: Critical: unbacked borrowing or solvency loss from misvaluation
- Fast validation: Exercise paths where price-type choice matters economically and assert the selected type matches protocol design for that exact action. Provide two plausible adapter contexts and assert the price path rejects every mix-and-match combination.
