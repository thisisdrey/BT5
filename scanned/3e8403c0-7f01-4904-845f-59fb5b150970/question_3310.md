# Q3310: TrustedCalls delegate execution: selector shape / cross-user action / exact whitelist semantics

## Question
Can an unprivileged delegate of its own Safe, with no authority over any victim Safe or privileged protocol role enter through `TrustedCalls.executeTrustedCall(...) and executeTrustedCallBatch(...)` with calldata whose first 4 bytes are trusted while the remaining payload is attacker-chosen while another Safe in the system has different delegates or target trust expectations and turn a delegate right meant for one Safe into value movement or state change affecting another user or Safe, breaking the rule that trusted-call checking should bind the exact operational semantics intended, not just the first 4 bytes in isolation and leading to Protocol state corruption reached through a supposedly trusted but actually overbroad delegate surface?

## Target
- File/function: contracts/TrustedCalls.sol / executeTrustedCall, executeTrustedCallBatch
- Entrypoint: TrustedCalls.executeTrustedCall(...) and executeTrustedCallBatch(...)
- Attacker controls: calldata whose first 4 bytes are trusted while the remaining payload is attacker-chosen
- Exploit idea: turn a delegate right meant for one Safe into value movement or state change affecting another user or Safe
- Invariant to test: trusted-call checking should bind the exact operational semantics intended, not just the first 4 bytes in isolation
- Expected Immunefi impact: Protocol state corruption reached through a supposedly trusted but actually overbroad delegate surface
- Fast validation: Fuzz trusted selectors with attacker-chosen trailing calldata and ensure the whitelist boundary cannot be widened by payload shape alone.
