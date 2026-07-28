# Q3277: TrustedCalls delegate execution: selector shape / cross-user action / per-safe auth

## Question
Can an unprivileged delegate of its own Safe, with no authority over any victim Safe or privileged protocol role enter through `TrustedCalls.executeTrustedCall(...) and executeTrustedCallBatch(...)` with calldata whose first 4 bytes are trusted while the remaining payload is attacker-chosen while the whitelist contains the target/selector pair for a legitimate operational path and turn a delegate right meant for one Safe into value movement or state change affecting another user or Safe, breaking the rule that a delegate authorization should apply only to the exact Safe that granted it and leading to Bypass of intended permissions and Safe-module access control?

## Target
- File/function: contracts/TrustedCalls.sol / executeTrustedCall, executeTrustedCallBatch
- Entrypoint: TrustedCalls.executeTrustedCall(...) and executeTrustedCallBatch(...)
- Attacker controls: calldata whose first 4 bytes are trusted while the remaining payload is attacker-chosen
- Exploit idea: turn a delegate right meant for one Safe into value movement or state change affecting another user or Safe
- Invariant to test: a delegate authorization should apply only to the exact Safe that granted it
- Expected Immunefi impact: Bypass of intended permissions and Safe-module access control
- Fast validation: Forge test one delegate address across multiple Safes and assert each execute path remains bound to the granting Safe only.
