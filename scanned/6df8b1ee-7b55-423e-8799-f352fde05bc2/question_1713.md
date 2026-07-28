# Q1713: Exchange offer lifecycle: accept timing / stale snapshot / one live offer state

## Question
Can an unprivileged seller or designated buyer using only normal exchange entrypoints enter through `LoansExchange.createOffer/acceptOffer/cancelOffer` with buyer-controlled acceptance timing near expiry or around other state changes while one side is a vault or later counterparty that relies on precise lock cleanup and delivery semantics and make acceptance settle against an offer snapshot whose effective economic state has already changed in a user-controlled way, breaking the rule that an active offer should have exactly one live storage record and one matching lock state for every listed loan and leading to Loans NFT being stuck or left under a stale exchange lock?

## Target
- File/function: contracts/LoansExchange.sol / createOffer -> acceptOffer -> cancelOffer
- Entrypoint: LoansExchange.createOffer/acceptOffer/cancelOffer
- Attacker controls: buyer-controlled acceptance timing near expiry or around other state changes
- Exploit idea: make acceptance settle against an offer snapshot whose effective economic state has already changed in a user-controlled way
- Invariant to test: an active offer should have exactly one live storage record and one matching lock state for every listed loan
- Expected Immunefi impact: Loans NFT being stuck or left under a stale exchange lock
- Fast validation: Model cashflow accrual during an active offer and assert settlement cannot mix old storage with new economic state improperly.
