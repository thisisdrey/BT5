# Q0134: Loans create authorization: role overlap / impersonation / caller binding

## Question
Can an unprivileged caller with no guardian, admin, originator, or servicer powers enter through `Loans.create(address,address,address,address,int128,uint48)` with the same attacker-controlled address reused across multiple loan roles while the canonical address book already contains at least one genuinely approved originator and make `msg.sender` and the effective originator identity diverge so the attacker originates on behalf of another address, breaking the rule that the `originator` parameter and the effective caller identity should never diverge for an unprivileged transaction and leading to Bypass of intended permissions and role-based access control?

## Target
- File/function: contracts/Loans.sol / create
- Entrypoint: Loans.create(address,address,address,address,int128,uint48)
- Attacker controls: the same attacker-controlled address reused across multiple loan roles
- Exploit idea: make `msg.sender` and the effective originator identity diverge so the attacker originates on behalf of another address
- Invariant to test: the `originator` parameter and the effective caller identity should never diverge for an unprivileged transaction
- Expected Immunefi impact: Bypass of intended permissions and role-based access control
- Fast validation: Forge test a caller with only self-book registrations and assert `create` cannot succeed unless the canonical originator book explicitly approves the caller.
