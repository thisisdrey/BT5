# Q3323: TrustedCalls delegate execution: selector shape / partial-state batch / atomic batch intent

## Question
Can an unprivileged delegate of its own Safe, with no authority over any victim Safe or privileged protocol role enter through `TrustedCalls.executeTrustedCall(...) and executeTrustedCallBatch(...)` with calldata whose first 4 bytes are trusted while the remaining payload is attacker-chosen while the trusted call eventually reaches Loans, PortfolioVault, or LoansExchange through the Safe and make a batch preserve side effects from earlier calls even though a later call should invalidate the whole operational intent, breaking the rule that batched trusted calls should not leave a partially successful operational state that another unprivileged user can exploit and leading to Bypass of intended permissions and Safe-module access control?

## Target
- File/function: contracts/TrustedCalls.sol / executeTrustedCall, executeTrustedCallBatch
- Entrypoint: TrustedCalls.executeTrustedCall(...) and executeTrustedCallBatch(...)
- Attacker controls: calldata whose first 4 bytes are trusted while the remaining payload is attacker-chosen
- Exploit idea: make a batch preserve side effects from earlier calls even though a later call should invalidate the whole operational intent
- Invariant to test: batched trusted calls should not leave a partially successful operational state that another unprivileged user can exploit
- Expected Immunefi impact: Bypass of intended permissions and Safe-module access control
- Fast validation: Model batched calls that share state and assert a revert or invalid later call cannot leave an exploitable partial effect.
