# Q3000: Address-book role bit isolation: book churn / book bleed / exact whitelist

## Question
Can an unprivileged address-book owner controlling only its own registrations enter through `LoansAuth.registerAddress(Roles,address) and unregisterAddress(Roles,address)` with rapid register/unregister cycles for the same grantee across different roles while the canonical `address(this)` book and a self-managed book both contain related addresses and make self-book updates affect canonical-book semantics or another address owner's semantics, breaking the rule that loan creation and exchange counterpart checks should require the exact intended role bit in the exact intended book and leading to Bypass of intended permissions and whitelisting rules?

## Target
- File/function: contracts/misc/LoansAuth.sol / registerAddress, unregisterAddress, isRegisteredForRole
- Entrypoint: LoansAuth.registerAddress(Roles,address) and unregisterAddress(Roles,address)
- Attacker controls: rapid register/unregister cycles for the same grantee across different roles
- Exploit idea: make self-book updates affect canonical-book semantics or another address owner's semantics
- Invariant to test: loan creation and exchange counterpart checks should require the exact intended role bit in the exact intended book
- Expected Immunefi impact: Bypass of intended permissions and whitelisting rules
- Fast validation: Model one self-managed book plus the canonical book and assert they can never substitute for each other.
