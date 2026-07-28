# Q0006: Loans create authorization: self-book spoof / impersonation / caller binding

## Question
Can an unprivileged caller with no guardian, admin, originator, or servicer powers enter through `Loans.create(address,address,address,address,int128,uint48)` with only a self-curated address book plus attacker-controlled borrower, investor, and servicer addresses while the canonical address book already contains at least one genuinely approved originator and make `msg.sender` and the effective originator identity diverge so the attacker originates on behalf of another address, breaking the rule that the `originator` parameter and the effective caller identity should never diverge for an unprivileged transaction and leading to Loans NFT being minted into an unauthorized economic context that can later harm another user?

## Target
- File/function: contracts/Loans.sol / create
- Entrypoint: Loans.create(address,address,address,address,int128,uint48)
- Attacker controls: only a self-curated address book plus attacker-controlled borrower, investor, and servicer addresses
- Exploit idea: make `msg.sender` and the effective originator identity diverge so the attacker originates on behalf of another address
- Invariant to test: the `originator` parameter and the effective caller identity should never diverge for an unprivileged transaction
- Expected Immunefi impact: Loans NFT being minted into an unauthorized economic context that can later harm another user
- Fast validation: Build a minimal scenario where a toxic loan would later be fundable or priceable, and assert the creation step itself cannot be reached by an unprivileged caller.
