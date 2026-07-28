# Q3242: TrustedCalls delegate execution: safe parameter / partial-state batch / exact whitelist semantics

## Question
Can an unprivileged delegate of its own Safe, with no authority over any victim Safe or privileged protocol role enter through `TrustedCalls.executeTrustedCall(...) and executeTrustedCallBatch(...)` with the `safe` parameter, target, and calldata bytes for a supposed trusted call while another Safe in the system has different delegates or target trust expectations and make a batch preserve side effects from earlier calls even though a later call should invalidate the whole operational intent, breaking the rule that trusted-call checking should bind the exact operational semantics intended, not just the first 4 bytes in isolation and leading to Theft or unauthorized movement of value from another Safe or user context?

## Target
- File/function: contracts/TrustedCalls.sol / executeTrustedCall, executeTrustedCallBatch
- Entrypoint: TrustedCalls.executeTrustedCall(...) and executeTrustedCallBatch(...)
- Attacker controls: the `safe` parameter, target, and calldata bytes for a supposed trusted call
- Exploit idea: make a batch preserve side effects from earlier calls even though a later call should invalidate the whole operational intent
- Invariant to test: trusted-call checking should bind the exact operational semantics intended, not just the first 4 bytes in isolation
- Expected Immunefi impact: Theft or unauthorized movement of value from another Safe or user context
- Fast validation: Check that a delegate of one Safe cannot drive Loans, Vault, or Exchange state as if acting for another Safe.
