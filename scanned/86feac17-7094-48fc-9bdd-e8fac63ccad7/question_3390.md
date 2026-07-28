# Q3390: TrustedCalls delegate execution: batch order / cross-user action / exact whitelist semantics

## Question
Can an unprivileged delegate of its own Safe, with no authority over any victim Safe or privileged protocol role enter through `TrustedCalls.executeTrustedCall(...) and executeTrustedCallBatch(...)` with the order of several batched trusted calls that share state across the same Safe or target while the trusted call eventually reaches Loans, PortfolioVault, or LoansExchange through the Safe and turn a delegate right meant for one Safe into value movement or state change affecting another user or Safe, breaking the rule that trusted-call checking should bind the exact operational semantics intended, not just the first 4 bytes in isolation and leading to Theft or unauthorized movement of value from another Safe or user context?

## Target
- File/function: contracts/TrustedCalls.sol / executeTrustedCall, executeTrustedCallBatch
- Entrypoint: TrustedCalls.executeTrustedCall(...) and executeTrustedCallBatch(...)
- Attacker controls: the order of several batched trusted calls that share state across the same Safe or target
- Exploit idea: turn a delegate right meant for one Safe into value movement or state change affecting another user or Safe
- Invariant to test: trusted-call checking should bind the exact operational semantics intended, not just the first 4 bytes in isolation
- Expected Immunefi impact: Theft or unauthorized movement of value from another Safe or user context
- Fast validation: Fuzz trusted selectors with attacker-chosen trailing calldata and ensure the whitelist boundary cannot be widened by payload shape alone.
