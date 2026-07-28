# Q3251: TrustedCalls delegate execution: safe parameter / safe confusion / atomic batch intent

## Question
Can an unprivileged delegate of its own Safe, with no authority over any victim Safe or privileged protocol role enter through `TrustedCalls.executeTrustedCall(...) and executeTrustedCallBatch(...)` with the `safe` parameter, target, and calldata bytes for a supposed trusted call while the trusted call eventually reaches Loans, PortfolioVault, or LoansExchange through the Safe and make a delegate of one Safe execute a call from another Safe or another trust context, breaking the rule that batched trusted calls should not leave a partially successful operational state that another unprivileged user can exploit and leading to Theft or unauthorized movement of value from another Safe or user context?

## Target
- File/function: contracts/TrustedCalls.sol / executeTrustedCall, executeTrustedCallBatch
- Entrypoint: TrustedCalls.executeTrustedCall(...) and executeTrustedCallBatch(...)
- Attacker controls: the `safe` parameter, target, and calldata bytes for a supposed trusted call
- Exploit idea: make a delegate of one Safe execute a call from another Safe or another trust context
- Invariant to test: batched trusted calls should not leave a partially successful operational state that another unprivileged user can exploit
- Expected Immunefi impact: Theft or unauthorized movement of value from another Safe or user context
- Fast validation: Fuzz trusted selectors with attacker-chosen trailing calldata and ensure the whitelist boundary cannot be widened by payload shape alone.
