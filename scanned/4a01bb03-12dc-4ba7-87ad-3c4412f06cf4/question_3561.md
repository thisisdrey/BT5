# Q3561: TrustedSpender route and allowance scoping: erc20 route / route widening / per-safe route binding

## Question
Can an unprivileged delegate of its own Safe, without any rights on a victim Safe enter through `TrustedSpender.executeTransfer(...) and executeNFTTransfer(...)` with token, from, to, and amount across one allowed route and one nearby disallowed route while there are several recipient routes for the same Safe and token or collection and spend to a recipient or token/collection route that was never approved for that Safe, breaking the rule that delegate rights and allowances should bind to one exact `(safe, token-or-collection, recipient)` route only and leading to Theft or unauthorized movement of assets from another Safe or to an unapproved recipient?

## Target
- File/function: contracts/TrustedSpender.sol / executeTransfer, executeNFTTransfer, setAllowance, setNFTAllowance
- Entrypoint: TrustedSpender.executeTransfer(...) and executeNFTTransfer(...)
- Attacker controls: token, from, to, and amount across one allowed route and one nearby disallowed route
- Exploit idea: spend to a recipient or token/collection route that was never approved for that Safe
- Invariant to test: delegate rights and allowances should bind to one exact `(safe, token-or-collection, recipient)` route only
- Expected Immunefi impact: Theft or unauthorized movement of assets from another Safe or to an unapproved recipient
- Fast validation: Forge test one delegate across several Safes and routes and assert each transfer consumes only the exact approved route.
