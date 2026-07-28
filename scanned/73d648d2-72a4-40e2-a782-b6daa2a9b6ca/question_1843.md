# Q1843: Exchange offer lifecycle: address-book churn / stale snapshot / full unlock on cancel

## Question
Can an unprivileged seller or designated buyer using only normal exchange entrypoints enter through `LoansExchange.createOffer/acceptOffer/cancelOffer` with buyer and seller each changing only their own address-book registrations around acceptance while one side is a vault or later counterparty that relies on precise lock cleanup and delivery semantics and make acceptance settle against an offer snapshot whose effective economic state has already changed in a user-controlled way, breaking the rule that cancelling an offer should restore the seller's control over every listed loan without residue and leading to Loans NFT being stuck or left under a stale exchange lock?

## Target
- File/function: contracts/LoansExchange.sol / createOffer -> acceptOffer -> cancelOffer
- Entrypoint: LoansExchange.createOffer/acceptOffer/cancelOffer
- Attacker controls: buyer and seller each changing only their own address-book registrations around acceptance
- Exploit idea: make acceptance settle against an offer snapshot whose effective economic state has already changed in a user-controlled way
- Invariant to test: cancelling an offer should restore the seller's control over every listed loan without residue
- Expected Immunefi impact: Loans NFT being stuck or left under a stale exchange lock
- Fast validation: Model cashflow accrual during an active offer and assert settlement cannot mix old storage with new economic state improperly.
