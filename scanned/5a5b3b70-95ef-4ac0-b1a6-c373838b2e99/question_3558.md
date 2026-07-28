# Q3558: TrustedSpender route and allowance scoping: erc20 route / expiry bypass / strict expiry

## Question
Can an unprivileged delegate of its own Safe, without any rights on a victim Safe enter through `TrustedSpender.executeTransfer(...) and executeNFTTransfer(...)` with token, from, to, and amount across one allowed route and one nearby disallowed route while there are several recipient routes for the same Safe and token or collection and spend after expiry or across an expiry edge when the route should no longer be valid, breaking the rule that expired allowances should never authorize any further ERC20 or ERC721 movement and leading to Theft or unauthorized movement of assets from another Safe or to an unapproved recipient?

## Target
- File/function: contracts/TrustedSpender.sol / executeTransfer, executeNFTTransfer, setAllowance, setNFTAllowance
- Entrypoint: TrustedSpender.executeTransfer(...) and executeNFTTransfer(...)
- Attacker controls: token, from, to, and amount across one allowed route and one nearby disallowed route
- Exploit idea: spend after expiry or across an expiry edge when the route should no longer be valid
- Invariant to test: expired allowances should never authorize any further ERC20 or ERC721 movement
- Expected Immunefi impact: Theft or unauthorized movement of assets from another Safe or to an unapproved recipient
- Fast validation: Forge test one delegate across several Safes and routes and assert each transfer consumes only the exact approved route.
