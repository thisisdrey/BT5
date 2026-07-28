# Q3368: TrustedCalls delegate execution: batch order / payload extension / no cross-safe bleed

## Question
Can an unprivileged delegate of its own Safe, with no authority over any victim Safe or privileged protocol role enter through `TrustedCalls.executeTrustedCall(...) and executeTrustedCallBatch(...)` with the order of several batched trusted calls that share state across the same Safe or target while another Safe in the system has different delegates or target trust expectations and use trusted selector matching to smuggle attacker-chosen semantics past the intended whitelist boundary, breaking the rule that a delegate or batch context should never bleed authority across Safe boundaries and leading to Bypass of intended permissions and Safe-module access control?

## Target
- File/function: contracts/TrustedCalls.sol / executeTrustedCall, executeTrustedCallBatch
- Entrypoint: TrustedCalls.executeTrustedCall(...) and executeTrustedCallBatch(...)
- Attacker controls: the order of several batched trusted calls that share state across the same Safe or target
- Exploit idea: use trusted selector matching to smuggle attacker-chosen semantics past the intended whitelist boundary
- Invariant to test: a delegate or batch context should never bleed authority across Safe boundaries
- Expected Immunefi impact: Bypass of intended permissions and Safe-module access control
- Fast validation: Forge test one delegate address across multiple Safes and assert each execute path remains bound to the granting Safe only.
