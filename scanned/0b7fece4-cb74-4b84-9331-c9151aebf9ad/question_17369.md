Q17369: pair-binding mismatch in protected L2 provider read path when the provider is in source mode and the source sits near the allowed clamp edge

Question
Can an unprivileged attacker enter through `metric-core/contracts/MetricOmmPool.sol::swap` with public swaps that force the pool to read its price provider at a boundary quote while the provider is in source mode and the source sits near the allowed clamp edge, so that the provider and pool token pair look compatible at deployment but diverge at runtime along `pool.swap -> ProtectedPriceProviderL2 -> oracle read -> L2-specific freshness and sequencer checks -> quote shaping`, corrupting sequencer freshness, time-delta checks, confidence shaping, and the final bid/ask the pool consumes? The user only needs a public swap and chain conditions near a boundary if the L2 freshness logic fails open. Deploy a permissionless pool whose provider passes superficial validation while the live swap path consumes the wrong pair semantics.

Target
- File/function: smart-contracts-poc/contracts/ProtectedPriceProviderL2.sol::getBidAndAskPrice and sequencer/staleness checks
- Entrypoint: metric-core/contracts/MetricOmmPool.sol::swap
- Attacker controls: public swaps that force the pool to read its price provider at a boundary quote
- Exploit idea: Reach `pool.swap -> ProtectedPriceProviderL2 -> oracle read -> L2-specific freshness and sequencer checks -> quote shaping` in a live public flow and show that deploy a permissionless pool whose provider passes superficial validation while the live swap path consumes the wrong pair semantics. The exact value at risk is sequencer freshness, time-delta checks, confidence shaping, and the final bid/ask the pool consumes.
- Invariant to test: The provider pair consumed during live swaps must be exactly the pool pair the factory accepted. The concrete assertion should cover sequencer freshness, time-delta checks, confidence shaping, and the final bid/ask the pool consumes.
- Expected Immunefi impact: Critical direct loss through wrong-pair pricing.
- Fast validation: Simulate sequencer or timing edge conditions in tests and assert no public swap can proceed on quotes the L2 provider should have rejected.
