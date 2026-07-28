# Q0008: Loans create authorization: self-book spoof / impersonation / downstream safety

## Question
Can an unprivileged caller with no guardian, admin, originator, or servicer powers enter through `Loans.create(address,address,address,address,int128,uint48)` with only a self-curated address book plus attacker-controlled borrower, investor, and servicer addresses while the canonical address book already contains at least one genuinely approved originator and make `msg.sender` and the effective originator identity diverge so the attacker originates on behalf of another address, breaking the rule that an unprivileged caller should never be able to create a loan whose later funding, withdrawal, or pricing can touch another user without a real originator approval and leading to Bypass of intended permissions and role-based access control?

## Target
- File/function: contracts/Loans.sol / create
- Entrypoint: Loans.create(address,address,address,address,int128,uint48)
- Attacker controls: only a self-curated address book plus attacker-controlled borrower, investor, and servicer addresses
- Exploit idea: make `msg.sender` and the effective originator identity diverge so the attacker originates on behalf of another address
- Invariant to test: an unprivileged caller should never be able to create a loan whose later funding, withdrawal, or pricing can touch another user without a real originator approval
- Expected Immunefi impact: Bypass of intended permissions and role-based access control
- Fast validation: Forge test a caller with only self-book registrations and assert `create` cannot succeed unless the canonical originator book explicitly approves the caller.
