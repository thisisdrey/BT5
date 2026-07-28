# Q3273: TrustedCalls delegate execution: selector shape / partial-state batch / per-safe auth

## Question
Can an unprivileged delegate of its own Safe, with no authority over any victim Safe or privileged protocol role enter through `TrustedCalls.executeTrustedCall(...) and executeTrustedCallBatch(...)` with calldata whose first 4 bytes are trusted while the remaining payload is attacker-chosen while the whitelist contains the target/selector pair for a legitimate operational path and make a batch preserve side effects from earlier calls even though a later call should invalidate the whole operational intent, breaking the rule that a delegate authorization should apply only to the exact Safe that granted it and leading to Protocol state corruption reached through a supposedly trusted but actually overbroad delegate surface?

## Target
- File/function: contracts/TrustedCalls.sol / executeTrustedCall, executeTrustedCallBatch
- Entrypoint: TrustedCalls.executeTrustedCall(...) and executeTrustedCallBatch(...)
- Attacker controls: calldata whose first 4 bytes are trusted while the remaining payload is attacker-chosen
- Exploit idea: make a batch preserve side effects from earlier calls even though a later call should invalidate the whole operational intent
- Invariant to test: a delegate authorization should apply only to the exact Safe that granted it
- Expected Immunefi impact: Protocol state corruption reached through a supposedly trusted but actually overbroad delegate surface
- Fast validation: Fuzz trusted selectors with attacker-chosen trailing calldata and ensure the whitelist boundary cannot be widened by payload shape alone.
