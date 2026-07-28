# Q3798: TrustedSpender route and allowance scoping: finite allowance / expiry bypass / strict expiry

## Question
Can an unprivileged delegate of its own Safe, without any rights on a victim Safe enter through `TrustedSpender.executeTransfer(...) and executeNFTTransfer(...)` with a finite allowance that the attacker tries to consume through repeated or interleaved calls while the route is configured with an infinite ERC20 allowance and long validity and spend after expiry or across an expiry edge when the route should no longer be valid, breaking the rule that expired allowances should never authorize any further ERC20 or ERC721 movement and leading to Protocol-facing unauthorized transfers that later harm another user or vault?

## Target
- File/function: contracts/TrustedSpender.sol / executeTransfer, executeNFTTransfer, setAllowance, setNFTAllowance
- Entrypoint: TrustedSpender.executeTransfer(...) and executeNFTTransfer(...)
- Attacker controls: a finite allowance that the attacker tries to consume through repeated or interleaved calls
- Exploit idea: spend after expiry or across an expiry edge when the route should no longer be valid
- Invariant to test: expired allowances should never authorize any further ERC20 or ERC721 movement
- Expected Immunefi impact: Protocol-facing unauthorized transfers that later harm another user or vault
- Fast validation: Check that neighboring recipients, collections, or tokens cannot be reached through a superficially similar approved route.
