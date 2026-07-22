Q19781: pair-binding mismatch in provider staleness and spread rejection when the provider uses a synthetic ratio between two oracle feeds

Question
Can an unprivileged attacker enter through `metric-core/contracts/MetricOmmPool.sol::swap` with live source-mode operation where the source quote sits near the anchor-band edge while the provider uses a synthetic ratio between two oracle feeds, so that the provider and pool token pair look compatible at deployment but diverge at runtime along `public swap -> provider read -> stale/spread/guard checks -> feed-stalled or live quote`, corrupting reference staleness, max-spread, price-guard checks, and whether the pool gets a rejected or accepted quote? A public attacker cannot corrupt the oracle, but can absolutely attempt trades when the quote is just about to become invalid. Deploy a permissionless pool whose provider passes superficial validation while the live swap path consumes the wrong pair semantics.

Target
- File/function: smart-contracts-poc/contracts/AnchoredPriceProvider.sol and smart-contracts-poc/contracts/ProtectedPriceProvider.sol rejection boundaries
- Entrypoint: metric-core/contracts/MetricOmmPool.sol::swap
- Attacker controls: live source-mode operation where the source quote sits near the anchor-band edge
- Exploit idea: Reach `public swap -> provider read -> stale/spread/guard checks -> feed-stalled or live quote` in a live public flow and show that deploy a permissionless pool whose provider passes superficial validation while the live swap path consumes the wrong pair semantics. The exact value at risk is reference staleness, max-spread, price-guard checks, and whether the pool gets a rejected or accepted quote.
- Invariant to test: The provider pair consumed during live swaps must be exactly the pool pair the factory accepted. The concrete assertion should cover reference staleness, max-spread, price-guard checks, and whether the pool gets a rejected or accepted quote.
- Expected Immunefi impact: Critical direct loss through wrong-pair pricing.
- Fast validation: Move oracle timestamps and spreads across their exact boundaries and assert every reachable quote transition is monotonic and fails closed.
