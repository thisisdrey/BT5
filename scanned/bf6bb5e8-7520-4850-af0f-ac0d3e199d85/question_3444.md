# Q3444: TrustedCalls delegate execution: shared delegate / safe confusion / no cross-safe bleed

## Question
Can an unprivileged delegate of its own Safe, with no authority over any victim Safe or privileged protocol role enter through `TrustedCalls.executeTrustedCall(...) and executeTrustedCallBatch(...)` with a delegate address reused across several Safes with different trust surfaces while the trusted call eventually reaches Loans, PortfolioVault, or LoansExchange through the Safe and make a delegate of one Safe execute a call from another Safe or another trust context, breaking the rule that a delegate or batch context should never bleed authority across Safe boundaries and leading to Theft or unauthorized movement of value from another Safe or user context?

## Target
- File/function: contracts/TrustedCalls.sol / executeTrustedCall, executeTrustedCallBatch
- Entrypoint: TrustedCalls.executeTrustedCall(...) and executeTrustedCallBatch(...)
- Attacker controls: a delegate address reused across several Safes with different trust surfaces
- Exploit idea: make a delegate of one Safe execute a call from another Safe or another trust context
- Invariant to test: a delegate or batch context should never bleed authority across Safe boundaries
- Expected Immunefi impact: Theft or unauthorized movement of value from another Safe or user context
- Fast validation: Fuzz trusted selectors with attacker-chosen trailing calldata and ensure the whitelist boundary cannot be widened by payload shape alone.
