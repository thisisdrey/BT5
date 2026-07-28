# Q3513: TrustedCalls delegate execution: return-data flow / partial-state batch / per-safe auth

## Question
Can an unprivileged delegate of its own Safe, with no authority over any victim Safe or privileged protocol role enter through `TrustedCalls.executeTrustedCall(...) and executeTrustedCallBatch(...)` with trusted targets whose return data may influence later calls in the same transaction while the trusted call eventually reaches Loans, PortfolioVault, or LoansExchange through the Safe and make a batch preserve side effects from earlier calls even though a later call should invalidate the whole operational intent, breaking the rule that a delegate authorization should apply only to the exact Safe that granted it and leading to Theft or unauthorized movement of value from another Safe or user context?

## Target
- File/function: contracts/TrustedCalls.sol / executeTrustedCall, executeTrustedCallBatch
- Entrypoint: TrustedCalls.executeTrustedCall(...) and executeTrustedCallBatch(...)
- Attacker controls: trusted targets whose return data may influence later calls in the same transaction
- Exploit idea: make a batch preserve side effects from earlier calls even though a later call should invalidate the whole operational intent
- Invariant to test: a delegate authorization should apply only to the exact Safe that granted it
- Expected Immunefi impact: Theft or unauthorized movement of value from another Safe or user context
- Fast validation: Fuzz trusted selectors with attacker-chosen trailing calldata and ensure the whitelist boundary cannot be widened by payload shape alone.
