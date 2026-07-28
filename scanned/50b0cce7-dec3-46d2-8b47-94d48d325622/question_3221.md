# Q3221: TrustedCalls delegate execution: safe parameter / payload extension / per-safe auth

## Question
Can an unprivileged delegate of its own Safe, with no authority over any victim Safe or privileged protocol role enter through `TrustedCalls.executeTrustedCall(...) and executeTrustedCallBatch(...)` with the `safe` parameter, target, and calldata bytes for a supposed trusted call while the batch mixes several individually trusted calls with shared state implications and use trusted selector matching to smuggle attacker-chosen semantics past the intended whitelist boundary, breaking the rule that a delegate authorization should apply only to the exact Safe that granted it and leading to Unintended or unfair fund distribution caused by cross-safe or cross-user execution confusion?

## Target
- File/function: contracts/TrustedCalls.sol / executeTrustedCall, executeTrustedCallBatch
- Entrypoint: TrustedCalls.executeTrustedCall(...) and executeTrustedCallBatch(...)
- Attacker controls: the `safe` parameter, target, and calldata bytes for a supposed trusted call
- Exploit idea: use trusted selector matching to smuggle attacker-chosen semantics past the intended whitelist boundary
- Invariant to test: a delegate authorization should apply only to the exact Safe that granted it
- Expected Immunefi impact: Unintended or unfair fund distribution caused by cross-safe or cross-user execution confusion
- Fast validation: Forge test one delegate address across multiple Safes and assert each execute path remains bound to the granting Safe only.
