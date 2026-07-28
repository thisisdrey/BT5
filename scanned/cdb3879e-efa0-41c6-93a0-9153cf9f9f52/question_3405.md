# Q3405: TrustedCalls delegate execution: shared delegate / cross-user action / per-safe auth

## Question
Can an unprivileged delegate of its own Safe, with no authority over any victim Safe or privileged protocol role enter through `TrustedCalls.executeTrustedCall(...) and executeTrustedCallBatch(...)` with a delegate address reused across several Safes with different trust surfaces while the whitelist contains the target/selector pair for a legitimate operational path and turn a delegate right meant for one Safe into value movement or state change affecting another user or Safe, breaking the rule that a delegate authorization should apply only to the exact Safe that granted it and leading to Unintended or unfair fund distribution caused by cross-safe or cross-user execution confusion?

## Target
- File/function: contracts/TrustedCalls.sol / executeTrustedCall, executeTrustedCallBatch
- Entrypoint: TrustedCalls.executeTrustedCall(...) and executeTrustedCallBatch(...)
- Attacker controls: a delegate address reused across several Safes with different trust surfaces
- Exploit idea: turn a delegate right meant for one Safe into value movement or state change affecting another user or Safe
- Invariant to test: a delegate authorization should apply only to the exact Safe that granted it
- Expected Immunefi impact: Unintended or unfair fund distribution caused by cross-safe or cross-user execution confusion
- Fast validation: Model batched calls that share state and assert a revert or invalid later call cannot leave an exploitable partial effect.
