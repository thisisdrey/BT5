# Q3425: TrustedCalls delegate execution: shared delegate / safe confusion / per-safe auth

## Question
Can an unprivileged delegate of its own Safe, with no authority over any victim Safe or privileged protocol role enter through `TrustedCalls.executeTrustedCall(...) and executeTrustedCallBatch(...)` with a delegate address reused across several Safes with different trust surfaces while another Safe in the system has different delegates or target trust expectations and make a delegate of one Safe execute a call from another Safe or another trust context, breaking the rule that a delegate authorization should apply only to the exact Safe that granted it and leading to Theft or unauthorized movement of value from another Safe or user context?

## Target
- File/function: contracts/TrustedCalls.sol / executeTrustedCall, executeTrustedCallBatch
- Entrypoint: TrustedCalls.executeTrustedCall(...) and executeTrustedCallBatch(...)
- Attacker controls: a delegate address reused across several Safes with different trust surfaces
- Exploit idea: make a delegate of one Safe execute a call from another Safe or another trust context
- Invariant to test: a delegate authorization should apply only to the exact Safe that granted it
- Expected Immunefi impact: Theft or unauthorized movement of value from another Safe or user context
- Fast validation: Check that a delegate of one Safe cannot drive Loans, Vault, or Exchange state as if acting for another Safe.
