# Q3315: TrustedCalls delegate execution: selector shape / safe confusion / atomic batch intent

## Question
Can an unprivileged delegate of its own Safe, with no authority over any victim Safe or privileged protocol role enter through `TrustedCalls.executeTrustedCall(...) and executeTrustedCallBatch(...)` with calldata whose first 4 bytes are trusted while the remaining payload is attacker-chosen while the trusted call eventually reaches Loans, PortfolioVault, or LoansExchange through the Safe and make a delegate of one Safe execute a call from another Safe or another trust context, breaking the rule that batched trusted calls should not leave a partially successful operational state that another unprivileged user can exploit and leading to Unintended or unfair fund distribution caused by cross-safe or cross-user execution confusion?

## Target
- File/function: contracts/TrustedCalls.sol / executeTrustedCall, executeTrustedCallBatch
- Entrypoint: TrustedCalls.executeTrustedCall(...) and executeTrustedCallBatch(...)
- Attacker controls: calldata whose first 4 bytes are trusted while the remaining payload is attacker-chosen
- Exploit idea: make a delegate of one Safe execute a call from another Safe or another trust context
- Invariant to test: batched trusted calls should not leave a partially successful operational state that another unprivileged user can exploit
- Expected Immunefi impact: Unintended or unfair fund distribution caused by cross-safe or cross-user execution confusion
- Fast validation: Forge test one delegate address across multiple Safes and assert each execute path remains bound to the granting Safe only.
