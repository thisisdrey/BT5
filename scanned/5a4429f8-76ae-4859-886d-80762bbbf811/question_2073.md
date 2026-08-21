# Q2073: Punch notification abuse in NewCalculatedRemotesFromConfig

## Question
Can an attacker use an advertised private/loopback remote in `NewCalculatedRemotesFromConfig` (calculated_remote.go) to make a node emit packets toward an arbitrary address of the attacker's choosing?

## Target
- File/function: `calculated_remote.go` -> `NewCalculatedRemotesFromConfig` (declared at calculated_remote.go:79)
- Entrypoint: Unauthenticated lighthouse/relay/handshake packet with attacker-chosen source address and advertised remotes
- Attacker controls: an advertised private/loopback remote; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send a punch notification naming a target address and count the resulting outbound packets.
- Invariant to test: Punch targets are limited to addresses associated with an authenticated peer relationship.
- Expected Immunefi impact: Reflection/amplification abuse of nodes against third-party targets.
- Fast validation: Integration test asserting `NewCalculatedRemotesFromConfig` sends nothing toward an address that has no authenticated relationship.
