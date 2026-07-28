# Q3224: TrustedCalls delegate execution: safe parameter / payload extension / no cross-safe bleed

## Question
Can an unprivileged delegate of its own Safe, with no authority over any victim Safe or privileged protocol role enter through `TrustedCalls.executeTrustedCall(...) and executeTrustedCallBatch(...)` with the `safe` parameter, target, and calldata bytes for a supposed trusted call while the batch mixes several individually trusted calls with shared state implications and use trusted selector matching to smuggle attacker-chosen semantics past the intended whitelist boundary, breaking the rule that a delegate or batch context should never bleed authority across Safe boundaries and leading to Theft or unauthorized movement of value from another Safe or user context?

## Target
- File/function: contracts/TrustedCalls.sol / executeTrustedCall, executeTrustedCallBatch
- Entrypoint: TrustedCalls.executeTrustedCall(...) and executeTrustedCallBatch(...)
- Attacker controls: the `safe` parameter, target, and calldata bytes for a supposed trusted call
- Exploit idea: use trusted selector matching to smuggle attacker-chosen semantics past the intended whitelist boundary
- Invariant to test: a delegate or batch context should never bleed authority across Safe boundaries
- Expected Immunefi impact: Theft or unauthorized movement of value from another Safe or user context
- Fast validation: Fuzz trusted selectors with attacker-chosen trailing calldata and ensure the whitelist boundary cannot be widened by payload shape alone.
