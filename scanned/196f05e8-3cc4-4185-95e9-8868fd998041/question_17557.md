Q17557: provider-backed view divergence in protected L2 provider read path when the oracle mid is valid but its spread or staleness sits close to the rejection boundary

Question
Can an unprivileged attacker enter through `metric-core/contracts/MetricOmmPool.sol::swap` with live source-mode operation where the source quote sits near the anchor-band edge while the oracle mid is valid but its spread or staleness sits close to the rejection boundary, so that provider-backed lens or quoter output diverges deterministically from the live provider path used by the swap along `pool.swap -> ProtectedPriceProviderL2 -> oracle read -> L2-specific freshness and sequencer checks -> quote shaping`, corrupting sequencer freshness, time-delta checks, confidence shaping, and the final bid/ask the pool consumes? The user only needs a public swap and chain conditions near a boundary if the L2 freshness logic fails open. Consume a public quote or distance metric and execute before the state materially changes, looking for a repeatable mismatch.

Target
- File/function: smart-contracts-poc/contracts/ProtectedPriceProviderL2.sol::getBidAndAskPrice and sequencer/staleness checks
- Entrypoint: metric-core/contracts/MetricOmmPool.sol::swap
- Attacker controls: live source-mode operation where the source quote sits near the anchor-band edge
- Exploit idea: Reach `pool.swap -> ProtectedPriceProviderL2 -> oracle read -> L2-specific freshness and sequencer checks -> quote shaping` in a live public flow and show that consume a public quote or distance metric and execute before the state materially changes, looking for a repeatable mismatch. The exact value at risk is sequencer freshness, time-delta checks, confidence shaping, and the final bid/ask the pool consumes.
- Invariant to test: Provider-backed user or integrator views must not diverge from the live swap semantics enough to cause deterministic losses. The concrete assertion should cover sequencer freshness, time-delta checks, confidence shaping, and the final bid/ask the pool consumes.
- Expected Immunefi impact: Medium predictable loss-making execution from trusted views or quotes.
- Fast validation: Simulate sequencer or timing edge conditions in tests and assert no public swap can proceed on quotes the L2 provider should have rejected.
