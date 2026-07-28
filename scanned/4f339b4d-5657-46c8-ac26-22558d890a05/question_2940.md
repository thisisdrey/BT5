# Q2940: Address-book role bit isolation: role reuse / whitelist bypass / exact whitelist

## Question
Can an unprivileged address-book owner controlling only its own registrations enter through `LoansAuth.registerAddress(Roles,address) and unregisterAddress(Roles,address)` with the same attacker-controlled grantee registered under several Roles values in the same book while the canonical `address(this)` book and a self-managed book both contain related addresses and make a counterparty pass a mutual-registration gate without the exact intended role bit being present, breaking the rule that loan creation and exchange counterpart checks should require the exact intended role bit in the exact intended book and leading to Bypass of intended permissions and whitelisting rules?

## Target
- File/function: contracts/misc/LoansAuth.sol / registerAddress, unregisterAddress, isRegisteredForRole
- Entrypoint: LoansAuth.registerAddress(Roles,address) and unregisterAddress(Roles,address)
- Attacker controls: the same attacker-controlled grantee registered under several Roles values in the same book
- Exploit idea: make a counterparty pass a mutual-registration gate without the exact intended role bit being present
- Invariant to test: loan creation and exchange counterpart checks should require the exact intended role bit in the exact intended book
- Expected Immunefi impact: Bypass of intended permissions and whitelisting rules
- Fast validation: Model one self-managed book plus the canonical book and assert they can never substitute for each other.
