# Q3365: TrustedCalls delegate execution: batch order / payload extension / per-safe auth

## Question
Can an unprivileged delegate of its own Safe, with no authority over any victim Safe or privileged protocol role enter through `TrustedCalls.executeTrustedCall(...) and executeTrustedCallBatch(...)` with the order of several batched trusted calls that share state across the same Safe or target while another Safe in the system has different delegates or target trust expectations and use trusted selector matching to smuggle attacker-chosen semantics past the intended whitelist boundary, breaking the rule that a delegate authorization should apply only to the exact Safe that granted it and leading to Theft or unauthorized movement of value from another Safe or user context?

## Target
- File/function: contracts/TrustedCalls.sol / executeTrustedCall, executeTrustedCallBatch
- Entrypoint: TrustedCalls.executeTrustedCall(...) and executeTrustedCallBatch(...)
- Attacker controls: the order of several batched trusted calls that share state across the same Safe or target
- Exploit idea: use trusted selector matching to smuggle attacker-chosen semantics past the intended whitelist boundary
- Invariant to test: a delegate authorization should apply only to the exact Safe that granted it
- Expected Immunefi impact: Theft or unauthorized movement of value from another Safe or user context
- Fast validation: Check that a delegate of one Safe cannot drive Loans, Vault, or Exchange state as if acting for another Safe.
