# Q3229: TrustedCalls delegate execution: safe parameter / cross-user action / per-safe auth

## Question
Can an unprivileged delegate of its own Safe, with no authority over any victim Safe or privileged protocol role enter through `TrustedCalls.executeTrustedCall(...) and executeTrustedCallBatch(...)` with the `safe` parameter, target, and calldata bytes for a supposed trusted call while the batch mixes several individually trusted calls with shared state implications and turn a delegate right meant for one Safe into value movement or state change affecting another user or Safe, breaking the rule that a delegate authorization should apply only to the exact Safe that granted it and leading to Bypass of intended permissions and Safe-module access control?

## Target
- File/function: contracts/TrustedCalls.sol / executeTrustedCall, executeTrustedCallBatch
- Entrypoint: TrustedCalls.executeTrustedCall(...) and executeTrustedCallBatch(...)
- Attacker controls: the `safe` parameter, target, and calldata bytes for a supposed trusted call
- Exploit idea: turn a delegate right meant for one Safe into value movement or state change affecting another user or Safe
- Invariant to test: a delegate authorization should apply only to the exact Safe that granted it
- Expected Immunefi impact: Bypass of intended permissions and Safe-module access control
- Fast validation: Model batched calls that share state and assert a revert or invalid later call cannot leave an exploitable partial effect.
