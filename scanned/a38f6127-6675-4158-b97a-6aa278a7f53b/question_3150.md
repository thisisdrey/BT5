# Q3150: Address-book role bit isolation: canonical adjacency / stale auth residue / book isolation

## Question
Can an unprivileged address-book owner controlling only its own registrations enter through `LoansAuth.registerAddress(Roles,address) and unregisterAddress(Roles,address)` with self-book actions performed near canonical-book admin changes but without any privileged access while a later `create` flow will read borrower, investor, or servicer bits from the chosen address book and make register/unregister churn leave a grantee usable in later flows after its intended role was supposedly removed, breaking the rule that a self-managed address book should never satisfy canonical-book checks or another owner's checks and leading to Bypass of intended permissions and whitelisting rules?

## Target
- File/function: contracts/misc/LoansAuth.sol / registerAddress, unregisterAddress, isRegisteredForRole
- Entrypoint: LoansAuth.registerAddress(Roles,address) and unregisterAddress(Roles,address)
- Attacker controls: self-book actions performed near canonical-book admin changes but without any privileged access
- Exploit idea: make register/unregister churn leave a grantee usable in later flows after its intended role was supposedly removed
- Invariant to test: a self-managed address book should never satisfy canonical-book checks or another owner's checks
- Expected Immunefi impact: Bypass of intended permissions and whitelisting rules
- Fast validation: Forge test role-bit combinations and assert each downstream check accepts only the exact role and exact book intended.
