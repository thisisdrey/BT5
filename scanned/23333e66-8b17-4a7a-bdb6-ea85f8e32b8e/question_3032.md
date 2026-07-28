# Q3032: Address-book role bit isolation: zero-like target / book bleed / exact whitelist

## Question
Can an unprivileged address-book owner controlling only its own registrations enter through `LoansAuth.registerAddress(Roles,address) and unregisterAddress(Roles,address)` with edge-case grantee addresses such as zero-like or contract addresses the attacker controls while a later `createOffer` or `acceptOffer` flow will read the investor bit from both sides' books and make self-book updates affect canonical-book semantics or another address owner's semantics, breaking the rule that loan creation and exchange counterpart checks should require the exact intended role bit in the exact intended book and leading to Loans NFT or cashflow rights entering an unauthorized counterparty context?

## Target
- File/function: contracts/misc/LoansAuth.sol / registerAddress, unregisterAddress, isRegisteredForRole
- Entrypoint: LoansAuth.registerAddress(Roles,address) and unregisterAddress(Roles,address)
- Attacker controls: edge-case grantee addresses such as zero-like or contract addresses the attacker controls
- Exploit idea: make self-book updates affect canonical-book semantics or another address owner's semantics
- Invariant to test: loan creation and exchange counterpart checks should require the exact intended role bit in the exact intended book
- Expected Immunefi impact: Loans NFT or cashflow rights entering an unauthorized counterparty context
- Fast validation: Check that mixed legitimate multi-role grantees do not accidentally satisfy unrelated authorization checks.
