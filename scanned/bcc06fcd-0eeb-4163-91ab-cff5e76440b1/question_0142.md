# Q0142: Loans create authorization: role overlap / toxic loan seed / caller binding

## Question
Can an unprivileged caller with no guardian, admin, originator, or servicer powers enter through `Loans.create(address,address,address,address,int128,uint48)` with the same attacker-controlled address reused across multiple loan roles while the canonical address book already contains at least one genuinely approved originator and seed a loan that looks valid on-chain even though the attacker never passed the intended canonical-originator gate, breaking the rule that the `originator` parameter and the effective caller identity should never diverge for an unprivileged transaction and leading to Loans NFT being minted into an unauthorized economic context that can later harm another user?

## Target
- File/function: contracts/Loans.sol / create
- Entrypoint: Loans.create(address,address,address,address,int128,uint48)
- Attacker controls: the same attacker-controlled address reused across multiple loan roles
- Exploit idea: seed a loan that looks valid on-chain even though the attacker never passed the intended canonical-originator gate
- Invariant to test: the `originator` parameter and the effective caller identity should never diverge for an unprivileged transaction
- Expected Immunefi impact: Loans NFT being minted into an unauthorized economic context that can later harm another user
- Fast validation: Build a minimal scenario where a toxic loan would later be fundable or priceable, and assert the creation step itself cannot be reached by an unprivileged caller.
