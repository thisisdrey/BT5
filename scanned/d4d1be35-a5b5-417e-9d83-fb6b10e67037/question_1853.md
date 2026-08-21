# Q1853: Punch notification abuse in hashPacket

## Question
Can an attacker use an oversized remote list in `hashPacket` (routing/balance.go) to make a node emit packets toward an arbitrary address of the attacker's choosing?

## Target
- File/function: `routing/balance.go` -> `hashPacket` (declared at routing/balance.go:14)
- Entrypoint: Unauthenticated lighthouse/relay/handshake packet with attacker-chosen source address and advertised remotes
- Attacker controls: an oversized remote list; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send a punch notification naming a target address and count the resulting outbound packets.
- Invariant to test: Punch targets are limited to addresses associated with an authenticated peer relationship.
- Expected Immunefi impact: Reflection/amplification abuse of nodes against third-party targets.
- Fast validation: Integration test asserting `hashPacket` sends nothing toward an address that has no authenticated relationship.
