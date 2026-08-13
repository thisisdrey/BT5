# Q2291: update_interest_rates: maintenance path leaves caches inconsistent with canonical state [a-bank-whose-totals-changed] [idempotence]

## Question
Can an unprivileged attacker use `lending_pool_accrue_bank_interest` with a bank whose totals changed between cache read and write in the same slot so `update_interest_rates` leaves caches inconsistent with canonical state in a way that later unlocks `Medium: durable financial inconsistency that enables later extraction or freezing` by violating `rate-cache updates must stay consistent with canonical bank totals and cannot be replayed or applied to the wrong state snapshot`? Focus specifically on whether the public helper is truly idempotent under unchanged state.

## Target
- File/function: `programs/marginfi/src/state/bank_cache.rs` / `update_interest_rates`
- Entrypoint: `lending_pool_accrue_bank_interest`
- Attacker controls: a bank whose totals changed between cache read and write in the same slot
- Exploit idea: Check refresh or update helpers that commit only part of the state that later value-moving instructions assume is coherent. Focus specifically on whether the public helper is truly idempotent under unchanged state.
- Invariant to test: rate-cache updates must stay consistent with canonical bank totals and cannot be replayed or applied to the wrong state snapshot
- Expected Immunefi impact: Medium: durable financial inconsistency that enables later extraction or freezing
- Fast validation: After the controlled maintenance call, immediately exercise dependent value-moving instructions and assert their acceptance exactly matches a fresh recomputation. Invoke the same helper repeatedly against unchanged state and assert protocol value, cache, and destinations do not drift.
