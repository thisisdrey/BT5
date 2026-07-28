# Q3292: TrustedCalls delegate execution: selector shape / partial-state batch / no cross-safe bleed

## Question
Can an unprivileged delegate of its own Safe, with no authority over any victim Safe or privileged protocol role enter through `TrustedCalls.executeTrustedCall(...) and executeTrustedCallBatch(...)` with calldata whose first 4 bytes are trusted while the remaining payload is attacker-chosen while the batch mixes several individually trusted calls with shared state implications and make a batch preserve side effects from earlier calls even though a later call should invalidate the whole operational intent, breaking the rule that a delegate or batch context should never bleed authority across Safe boundaries and leading to Protocol state corruption reached through a supposedly trusted but actually overbroad delegate surface?

## Target
- File/function: contracts/TrustedCalls.sol / executeTrustedCall, executeTrustedCallBatch
- Entrypoint: TrustedCalls.executeTrustedCall(...) and executeTrustedCallBatch(...)
- Attacker controls: calldata whose first 4 bytes are trusted while the remaining payload is attacker-chosen
- Exploit idea: make a batch preserve side effects from earlier calls even though a later call should invalidate the whole operational intent
- Invariant to test: a delegate or batch context should never bleed authority across Safe boundaries
- Expected Immunefi impact: Protocol state corruption reached through a supposedly trusted but actually overbroad delegate surface
- Fast validation: Check that a delegate of one Safe cannot drive Loans, Vault, or Exchange state as if acting for another Safe.
