# Q3382: TrustedCalls delegate execution: batch order / payload extension / exact whitelist semantics

## Question
Can an unprivileged delegate of its own Safe, with no authority over any victim Safe or privileged protocol role enter through `TrustedCalls.executeTrustedCall(...) and executeTrustedCallBatch(...)` with the order of several batched trusted calls that share state across the same Safe or target while the trusted call eventually reaches Loans, PortfolioVault, or LoansExchange through the Safe and use trusted selector matching to smuggle attacker-chosen semantics past the intended whitelist boundary, breaking the rule that trusted-call checking should bind the exact operational semantics intended, not just the first 4 bytes in isolation and leading to Protocol state corruption reached through a supposedly trusted but actually overbroad delegate surface?

## Target
- File/function: contracts/TrustedCalls.sol / executeTrustedCall, executeTrustedCallBatch
- Entrypoint: TrustedCalls.executeTrustedCall(...) and executeTrustedCallBatch(...)
- Attacker controls: the order of several batched trusted calls that share state across the same Safe or target
- Exploit idea: use trusted selector matching to smuggle attacker-chosen semantics past the intended whitelist boundary
- Invariant to test: trusted-call checking should bind the exact operational semantics intended, not just the first 4 bytes in isolation
- Expected Immunefi impact: Protocol state corruption reached through a supposedly trusted but actually overbroad delegate surface
- Fast validation: Check that a delegate of one Safe cannot drive Loans, Vault, or Exchange state as if acting for another Safe.
