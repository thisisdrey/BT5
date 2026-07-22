Q17277: synthetic ratio direction error in protected L2 provider read path when the oracle mid is valid but its spread or staleness sits close to the rejection boundary

Question
Can an unprivileged attacker enter through `metric-core/contracts/MetricOmmPool.sol::swap` with live source-mode operation where the source quote sits near the anchor-band edge while the oracle mid is valid but its spread or staleness sits close to the rejection boundary, so that two-feed ratio mode uses the right feeds but the wrong direction, spread composition, or rounding convention along `pool.swap -> ProtectedPriceProviderL2 -> oracle read -> L2-specific freshness and sequencer checks -> quote shaping`, corrupting sequencer freshness, time-delta checks, confidence shaping, and the final bid/ask the pool consumes? The user only needs a public swap and chain conditions near a boundary if the L2 freshness logic fails open. Trigger a public swap through a synthetically priced pool and see whether the ratio quote moves opposite to the intended pair orientation.

Target
- File/function: smart-contracts-poc/contracts/ProtectedPriceProviderL2.sol::getBidAndAskPrice and sequencer/staleness checks
- Entrypoint: metric-core/contracts/MetricOmmPool.sol::swap
- Attacker controls: live source-mode operation where the source quote sits near the anchor-band edge
- Exploit idea: Reach `pool.swap -> ProtectedPriceProviderL2 -> oracle read -> L2-specific freshness and sequencer checks -> quote shaping` in a live public flow and show that trigger a public swap through a synthetically priced pool and see whether the ratio quote moves opposite to the intended pair orientation. The exact value at risk is sequencer freshness, time-delta checks, confidence shaping, and the final bid/ask the pool consumes.
- Invariant to test: Synthetic provider mode must preserve pair direction and bounded spread exactly as documented. The concrete assertion should cover sequencer freshness, time-delta checks, confidence shaping, and the final bid/ask the pool consumes.
- Expected Immunefi impact: Critical direct loss if pools trade against an inverted or materially wrong synthetic quote.
- Fast validation: Simulate sequencer or timing edge conditions in tests and assert no public swap can proceed on quotes the L2 provider should have rejected.
