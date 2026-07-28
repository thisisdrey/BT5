# Q2986: Address-book role bit isolation: book churn / whitelist bypass / book isolation

## Question
Can an unprivileged address-book owner controlling only its own registrations enter through `LoansAuth.registerAddress(Roles,address) and unregisterAddress(Roles,address)` with rapid register/unregister cycles for the same grantee across different roles while the same grantee address is intentionally present under multiple roles for legitimate reasons and make a counterparty pass a mutual-registration gate without the exact intended role bit being present, breaking the rule that a self-managed address book should never satisfy canonical-book checks or another owner's checks and leading to Unintended or unfair fund distribution after a wrong-role counterparty enters the lifecycle?

## Target
- File/function: contracts/misc/LoansAuth.sol / registerAddress, unregisterAddress, isRegisteredForRole
- Entrypoint: LoansAuth.registerAddress(Roles,address) and unregisterAddress(Roles,address)
- Attacker controls: rapid register/unregister cycles for the same grantee across different roles
- Exploit idea: make a counterparty pass a mutual-registration gate without the exact intended role bit being present
- Invariant to test: a self-managed address book should never satisfy canonical-book checks or another owner's checks
- Expected Immunefi impact: Unintended or unfair fund distribution after a wrong-role counterparty enters the lifecycle
- Fast validation: Model one self-managed book plus the canonical book and assert they can never substitute for each other.
