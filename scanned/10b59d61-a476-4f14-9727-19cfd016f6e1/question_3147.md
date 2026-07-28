# Q3147: Address-book role bit isolation: canonical adjacency / whitelist bypass / precise unregister

## Question
Can an unprivileged address-book owner controlling only its own registrations enter through `LoansAuth.registerAddress(Roles,address) and unregisterAddress(Roles,address)` with self-book actions performed near canonical-book admin changes but without any privileged access while a later `create` flow will read borrower, investor, or servicer bits from the chosen address book and make a counterparty pass a mutual-registration gate without the exact intended role bit being present, breaking the rule that unregistering one role should not leave or remove unrelated role authority unexpectedly and leading to Bypass of intended permissions and whitelisting rules?

## Target
- File/function: contracts/misc/LoansAuth.sol / registerAddress, unregisterAddress, isRegisteredForRole
- Entrypoint: LoansAuth.registerAddress(Roles,address) and unregisterAddress(Roles,address)
- Attacker controls: self-book actions performed near canonical-book admin changes but without any privileged access
- Exploit idea: make a counterparty pass a mutual-registration gate without the exact intended role bit being present
- Invariant to test: unregistering one role should not leave or remove unrelated role authority unexpectedly
- Expected Immunefi impact: Bypass of intended permissions and whitelisting rules
- Fast validation: Forge test role-bit combinations and assert each downstream check accepts only the exact role and exact book intended.
