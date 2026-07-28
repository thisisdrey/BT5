# Q2936: Address-book role bit isolation: role reuse / book bleed / exact whitelist

## Question
Can an unprivileged address-book owner controlling only its own registrations enter through `LoansAuth.registerAddress(Roles,address) and unregisterAddress(Roles,address)` with the same attacker-controlled grantee registered under several Roles values in the same book while the canonical `address(this)` book and a self-managed book both contain related addresses and make self-book updates affect canonical-book semantics or another address owner's semantics, breaking the rule that loan creation and exchange counterpart checks should require the exact intended role bit in the exact intended book and leading to Loans NFT or cashflow rights entering an unauthorized counterparty context?

## Target
- File/function: contracts/misc/LoansAuth.sol / registerAddress, unregisterAddress, isRegisteredForRole
- Entrypoint: LoansAuth.registerAddress(Roles,address) and unregisterAddress(Roles,address)
- Attacker controls: the same attacker-controlled grantee registered under several Roles values in the same book
- Exploit idea: make self-book updates affect canonical-book semantics or another address owner's semantics
- Invariant to test: loan creation and exchange counterpart checks should require the exact intended role bit in the exact intended book
- Expected Immunefi impact: Loans NFT or cashflow rights entering an unauthorized counterparty context
- Fast validation: Check that mixed legitimate multi-role grantees do not accidentally satisfy unrelated authorization checks.
