# Q3504: TrustedCalls delegate execution: return-data flow / cross-user action / no cross-safe bleed

## Question
Can an unprivileged delegate of its own Safe, with no authority over any victim Safe or privileged protocol role enter through `TrustedCalls.executeTrustedCall(...) and executeTrustedCallBatch(...)` with trusted targets whose return data may influence later calls in the same transaction while another Safe in the system has different delegates or target trust expectations and turn a delegate right meant for one Safe into value movement or state change affecting another user or Safe, breaking the rule that a delegate or batch context should never bleed authority across Safe boundaries and leading to Bypass of intended permissions and Safe-module access control?

## Target
- File/function: contracts/TrustedCalls.sol / executeTrustedCall, executeTrustedCallBatch
- Entrypoint: TrustedCalls.executeTrustedCall(...) and executeTrustedCallBatch(...)
- Attacker controls: trusted targets whose return data may influence later calls in the same transaction
- Exploit idea: turn a delegate right meant for one Safe into value movement or state change affecting another user or Safe
- Invariant to test: a delegate or batch context should never bleed authority across Safe boundaries
- Expected Immunefi impact: Bypass of intended permissions and Safe-module access control
- Fast validation: Forge test one delegate address across multiple Safes and assert each execute path remains bound to the granting Safe only.
