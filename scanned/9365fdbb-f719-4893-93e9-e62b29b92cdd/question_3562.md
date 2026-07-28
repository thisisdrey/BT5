# Q3562: TrustedSpender route and allowance scoping: erc20 route / route widening / strict expiry

## Question
Can an unprivileged delegate of its own Safe, without any rights on a victim Safe enter through `TrustedSpender.executeTransfer(...) and executeNFTTransfer(...)` with token, from, to, and amount across one allowed route and one nearby disallowed route while there are several recipient routes for the same Safe and token or collection and spend to a recipient or token/collection route that was never approved for that Safe, breaking the rule that expired allowances should never authorize any further ERC20 or ERC721 movement and leading to Bypass of intended permissions and allowance scoping?

## Target
- File/function: contracts/TrustedSpender.sol / executeTransfer, executeNFTTransfer, setAllowance, setNFTAllowance
- Entrypoint: TrustedSpender.executeTransfer(...) and executeNFTTransfer(...)
- Attacker controls: token, from, to, and amount across one allowed route and one nearby disallowed route
- Exploit idea: spend to a recipient or token/collection route that was never approved for that Safe
- Invariant to test: expired allowances should never authorize any further ERC20 or ERC721 movement
- Expected Immunefi impact: Bypass of intended permissions and allowance scoping
- Fast validation: Check that neighboring recipients, collections, or tokens cannot be reached through a superficially similar approved route.
