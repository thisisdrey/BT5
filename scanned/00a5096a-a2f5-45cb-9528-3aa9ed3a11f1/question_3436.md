# Q3436: TrustedCalls delegate execution: shared delegate / partial-state batch / no cross-safe bleed

## Question
Can an unprivileged delegate of its own Safe, with no authority over any victim Safe or privileged protocol role enter through `TrustedCalls.executeTrustedCall(...) and executeTrustedCallBatch(...)` with a delegate address reused across several Safes with different trust surfaces while another Safe in the system has different delegates or target trust expectations and make a batch preserve side effects from earlier calls even though a later call should invalidate the whole operational intent, breaking the rule that a delegate or batch context should never bleed authority across Safe boundaries and leading to Unintended or unfair fund distribution caused by cross-safe or cross-user execution confusion?

## Target
- File/function: contracts/TrustedCalls.sol / executeTrustedCall, executeTrustedCallBatch
- Entrypoint: TrustedCalls.executeTrustedCall(...) and executeTrustedCallBatch(...)
- Attacker controls: a delegate address reused across several Safes with different trust surfaces
- Exploit idea: make a batch preserve side effects from earlier calls even though a later call should invalidate the whole operational intent
- Invariant to test: a delegate or batch context should never bleed authority across Safe boundaries
- Expected Immunefi impact: Unintended or unfair fund distribution caused by cross-safe or cross-user execution confusion
- Fast validation: Model batched calls that share state and assert a revert or invalid later call cannot leave an exploitable partial effect.
