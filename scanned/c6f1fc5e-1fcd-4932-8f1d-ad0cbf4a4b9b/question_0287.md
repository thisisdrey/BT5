# Q0287: Loans create authorization: approval race / toxic loan seed / role isolation

## Question
Can an unprivileged caller with no guardian, admin, originator, or servicer powers enter through `Loans.create(address,address,address,address,int128,uint48)` with a normal transaction ordering race around canonical originator approval and self-registration while the attacker has only self-registered role bits in its own address book and no canonical approvals and seed a loan that looks valid on-chain even though the attacker never passed the intended canonical-originator gate, breaking the rule that borrower, investor, servicer, and originator authorization should come from the intended book and role only and leading to Loans NFT being minted into an unauthorized economic context that can later harm another user?

## Target
- File/function: contracts/Loans.sol / create
- Entrypoint: Loans.create(address,address,address,address,int128,uint48)
- Attacker controls: a normal transaction ordering race around canonical originator approval and self-registration
- Exploit idea: seed a loan that looks valid on-chain even though the attacker never passed the intended canonical-originator gate
- Invariant to test: borrower, investor, servicer, and originator authorization should come from the intended book and role only
- Expected Immunefi impact: Loans NFT being minted into an unauthorized economic context that can later harm another user
- Fast validation: Forge test a caller with only self-book registrations and assert `create` cannot succeed unless the canonical originator book explicitly approves the caller.
