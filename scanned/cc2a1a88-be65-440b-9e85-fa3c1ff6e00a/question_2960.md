# Q2960: Address-book role bit isolation: book churn / stale auth residue / exact whitelist

## Question
Can an unprivileged address-book owner controlling only its own registrations enter through `LoansAuth.registerAddress(Roles,address) and unregisterAddress(Roles,address)` with rapid register/unregister cycles for the same grantee across different roles while a later `create` flow will read borrower, investor, or servicer bits from the chosen address book and make register/unregister churn leave a grantee usable in later flows after its intended role was supposedly removed, breaking the rule that loan creation and exchange counterpart checks should require the exact intended role bit in the exact intended book and leading to Loans NFT or cashflow rights entering an unauthorized counterparty context?

## Target
- File/function: contracts/misc/LoansAuth.sol / registerAddress, unregisterAddress, isRegisteredForRole
- Entrypoint: LoansAuth.registerAddress(Roles,address) and unregisterAddress(Roles,address)
- Attacker controls: rapid register/unregister cycles for the same grantee across different roles
- Exploit idea: make register/unregister churn leave a grantee usable in later flows after its intended role was supposedly removed
- Invariant to test: loan creation and exchange counterpart checks should require the exact intended role bit in the exact intended book
- Expected Immunefi impact: Loans NFT or cashflow rights entering an unauthorized counterparty context
- Fast validation: Fuzz register/unregister churn across roles and ensure no stale authorization survives in create or exchange flows.
