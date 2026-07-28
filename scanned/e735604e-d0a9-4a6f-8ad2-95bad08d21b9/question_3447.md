# Q3447: TrustedCalls delegate execution: shared delegate / payload extension / atomic batch intent

## Question
Can an unprivileged delegate of its own Safe, with no authority over any victim Safe or privileged protocol role enter through `TrustedCalls.executeTrustedCall(...) and executeTrustedCallBatch(...)` with a delegate address reused across several Safes with different trust surfaces while the trusted call eventually reaches Loans, PortfolioVault, or LoansExchange through the Safe and use trusted selector matching to smuggle attacker-chosen semantics past the intended whitelist boundary, breaking the rule that batched trusted calls should not leave a partially successful operational state that another unprivileged user can exploit and leading to Theft or unauthorized movement of value from another Safe or user context?

## Target
- File/function: contracts/TrustedCalls.sol / executeTrustedCall, executeTrustedCallBatch
- Entrypoint: TrustedCalls.executeTrustedCall(...) and executeTrustedCallBatch(...)
- Attacker controls: a delegate address reused across several Safes with different trust surfaces
- Exploit idea: use trusted selector matching to smuggle attacker-chosen semantics past the intended whitelist boundary
- Invariant to test: batched trusted calls should not leave a partially successful operational state that another unprivileged user can exploit
- Expected Immunefi impact: Theft or unauthorized movement of value from another Safe or user context
- Fast validation: Fuzz trusted selectors with attacker-chosen trailing calldata and ensure the whitelist boundary cannot be widened by payload shape alone.
