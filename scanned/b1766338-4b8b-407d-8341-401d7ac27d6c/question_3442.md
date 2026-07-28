# Q3442: TrustedCalls delegate execution: shared delegate / safe confusion / exact whitelist semantics

## Question
Can an unprivileged delegate of its own Safe, with no authority over any victim Safe or privileged protocol role enter through `TrustedCalls.executeTrustedCall(...) and executeTrustedCallBatch(...)` with a delegate address reused across several Safes with different trust surfaces while the trusted call eventually reaches Loans, PortfolioVault, or LoansExchange through the Safe and make a delegate of one Safe execute a call from another Safe or another trust context, breaking the rule that trusted-call checking should bind the exact operational semantics intended, not just the first 4 bytes in isolation and leading to Protocol state corruption reached through a supposedly trusted but actually overbroad delegate surface?

## Target
- File/function: contracts/TrustedCalls.sol / executeTrustedCall, executeTrustedCallBatch
- Entrypoint: TrustedCalls.executeTrustedCall(...) and executeTrustedCallBatch(...)
- Attacker controls: a delegate address reused across several Safes with different trust surfaces
- Exploit idea: make a delegate of one Safe execute a call from another Safe or another trust context
- Invariant to test: trusted-call checking should bind the exact operational semantics intended, not just the first 4 bytes in isolation
- Expected Immunefi impact: Protocol state corruption reached through a supposedly trusted but actually overbroad delegate surface
- Fast validation: Check that a delegate of one Safe cannot drive Loans, Vault, or Exchange state as if acting for another Safe.
