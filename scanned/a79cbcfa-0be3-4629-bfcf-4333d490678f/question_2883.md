# Q2883: Address-book role bit isolation: role reuse / bit confusion / precise unregister

## Question
Can an unprivileged address-book owner controlling only its own registrations enter through `LoansAuth.registerAddress(Roles,address) and unregisterAddress(Roles,address)` with the same attacker-controlled grantee registered under several Roles values in the same book while a later `create` flow will read borrower, investor, or servicer bits from the chosen address book and make one role bit satisfy another role check or leave stale authorization after unregistering the wrong role, breaking the rule that unregistering one role should not leave or remove unrelated role authority unexpectedly and leading to Unintended or unfair fund distribution after a wrong-role counterparty enters the lifecycle?

## Target
- File/function: contracts/misc/LoansAuth.sol / registerAddress, unregisterAddress, isRegisteredForRole
- Entrypoint: LoansAuth.registerAddress(Roles,address) and unregisterAddress(Roles,address)
- Attacker controls: the same attacker-controlled grantee registered under several Roles values in the same book
- Exploit idea: make one role bit satisfy another role check or leave stale authorization after unregistering the wrong role
- Invariant to test: unregistering one role should not leave or remove unrelated role authority unexpectedly
- Expected Immunefi impact: Unintended or unfair fund distribution after a wrong-role counterparty enters the lifecycle
- Fast validation: Model one self-managed book plus the canonical book and assert they can never substitute for each other.
