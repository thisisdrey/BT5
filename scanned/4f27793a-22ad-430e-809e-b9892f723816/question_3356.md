# Q3356: TrustedCalls delegate execution: batch order / partial-state batch / no cross-safe bleed

## Question
Can an unprivileged delegate of its own Safe, with no authority over any victim Safe or privileged protocol role enter through `TrustedCalls.executeTrustedCall(...) and executeTrustedCallBatch(...)` with the order of several batched trusted calls that share state across the same Safe or target while the batch mixes several individually trusted calls with shared state implications and make a batch preserve side effects from earlier calls even though a later call should invalidate the whole operational intent, breaking the rule that a delegate or batch context should never bleed authority across Safe boundaries and leading to Bypass of intended permissions and Safe-module access control?

## Target
- File/function: contracts/TrustedCalls.sol / executeTrustedCall, executeTrustedCallBatch
- Entrypoint: TrustedCalls.executeTrustedCall(...) and executeTrustedCallBatch(...)
- Attacker controls: the order of several batched trusted calls that share state across the same Safe or target
- Exploit idea: make a batch preserve side effects from earlier calls even though a later call should invalidate the whole operational intent
- Invariant to test: a delegate or batch context should never bleed authority across Safe boundaries
- Expected Immunefi impact: Bypass of intended permissions and Safe-module access control
- Fast validation: Model batched calls that share state and assert a revert or invalid later call cannot leave an exploitable partial effect.
