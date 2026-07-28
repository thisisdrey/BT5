# Q3403: TrustedCalls delegate execution: shared delegate / partial-state batch / atomic batch intent

## Question
Can an unprivileged delegate of its own Safe, with no authority over any victim Safe or privileged protocol role enter through `TrustedCalls.executeTrustedCall(...) and executeTrustedCallBatch(...)` with a delegate address reused across several Safes with different trust surfaces while the whitelist contains the target/selector pair for a legitimate operational path and make a batch preserve side effects from earlier calls even though a later call should invalidate the whole operational intent, breaking the rule that batched trusted calls should not leave a partially successful operational state that another unprivileged user can exploit and leading to Protocol state corruption reached through a supposedly trusted but actually overbroad delegate surface?

## Target
- File/function: contracts/TrustedCalls.sol / executeTrustedCall, executeTrustedCallBatch
- Entrypoint: TrustedCalls.executeTrustedCall(...) and executeTrustedCallBatch(...)
- Attacker controls: a delegate address reused across several Safes with different trust surfaces
- Exploit idea: make a batch preserve side effects from earlier calls even though a later call should invalidate the whole operational intent
- Invariant to test: batched trusted calls should not leave a partially successful operational state that another unprivileged user can exploit
- Expected Immunefi impact: Protocol state corruption reached through a supposedly trusted but actually overbroad delegate surface
- Fast validation: Fuzz trusted selectors with attacker-chosen trailing calldata and ensure the whitelist boundary cannot be widened by payload shape alone.
