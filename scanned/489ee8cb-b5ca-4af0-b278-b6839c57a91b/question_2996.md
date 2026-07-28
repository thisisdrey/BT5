# Q2996: Address-book role bit isolation: book churn / bit confusion / exact whitelist

## Question
Can an unprivileged address-book owner controlling only its own registrations enter through `LoansAuth.registerAddress(Roles,address) and unregisterAddress(Roles,address)` with rapid register/unregister cycles for the same grantee across different roles while the canonical `address(this)` book and a self-managed book both contain related addresses and make one role bit satisfy another role check or leave stale authorization after unregistering the wrong role, breaking the rule that loan creation and exchange counterpart checks should require the exact intended role bit in the exact intended book and leading to Loans NFT or cashflow rights entering an unauthorized counterparty context?

## Target
- File/function: contracts/misc/LoansAuth.sol / registerAddress, unregisterAddress, isRegisteredForRole
- Entrypoint: LoansAuth.registerAddress(Roles,address) and unregisterAddress(Roles,address)
- Attacker controls: rapid register/unregister cycles for the same grantee across different roles
- Exploit idea: make one role bit satisfy another role check or leave stale authorization after unregistering the wrong role
- Invariant to test: loan creation and exchange counterpart checks should require the exact intended role bit in the exact intended book
- Expected Immunefi impact: Loans NFT or cashflow rights entering an unauthorized counterparty context
- Fast validation: Check that mixed legitimate multi-role grantees do not accidentally satisfy unrelated authorization checks.
