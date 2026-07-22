Q17434: Q64 or decimals mismatch in protected L2 provider read path when the oracle mid is valid but its spread or staleness sits close to the rejection boundary

Question
Can an unprivileged attacker enter through `metric-core/contracts/MetricOmmPool.sol::swap` with swap direction and `priceLimitX64` choices around the current oracle mid while the oracle mid is valid but its spread or staleness sits close to the rejection boundary, so that oracle 8-decimal values and Q64.64 pool prices stop agreeing under a reachable rounding or decimal edge case along `pool.swap -> ProtectedPriceProviderL2 -> oracle read -> L2-specific freshness and sequencer checks -> quote shaping`, corrupting sequencer freshness, time-delta checks, confidence shaping, and the final bid/ask the pool consumes? The user only needs a public swap and chain conditions near a boundary if the L2 freshness logic fails open. Use standard tokens and public trade sizes that push the provider conversion right onto a truncation boundary.

Target
- File/function: smart-contracts-poc/contracts/ProtectedPriceProviderL2.sol::getBidAndAskPrice and sequencer/staleness checks
- Entrypoint: metric-core/contracts/MetricOmmPool.sol::swap
- Attacker controls: swap direction and `priceLimitX64` choices around the current oracle mid
- Exploit idea: Reach `pool.swap -> ProtectedPriceProviderL2 -> oracle read -> L2-specific freshness and sequencer checks -> quote shaping` in a live public flow and show that use standard tokens and public trade sizes that push the provider conversion right onto a truncation boundary. The exact value at risk is sequencer freshness, time-delta checks, confidence shaping, and the final bid/ask the pool consumes.
- Invariant to test: Provider conversions between oracle decimals and Q64.64 must preserve order, monotonicity, and safe rounding. The concrete assertion should cover sequencer freshness, time-delta checks, confidence shaping, and the final bid/ask the pool consumes.
- Expected Immunefi impact: Medium/High loss-making swaps or LP value leakage above contest thresholds.
- Fast validation: Simulate sequencer or timing edge conditions in tests and assert no public swap can proceed on quotes the L2 provider should have rejected.
