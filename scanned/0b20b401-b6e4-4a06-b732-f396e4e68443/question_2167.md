# Q2167: Punch notification abuse in newCalculatedRemotesEntryFromConfig

## Question
Can an attacker use a relay request for a host it does not own in `newCalculatedRemotesEntryFromConfig` (calculated_remote.go) to make a node emit packets toward an arbitrary address of the attacker's choosing?

## Target
- File/function: `calculated_remote.go` -> `newCalculatedRemotesEntryFromConfig` (declared at calculated_remote.go:126)
- Entrypoint: Unauthenticated lighthouse/relay/handshake packet with attacker-chosen source address and advertised remotes
- Attacker controls: a relay request for a host it does not own; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send a punch notification naming a target address and count the resulting outbound packets.
- Invariant to test: Punch targets are limited to addresses associated with an authenticated peer relationship.
- Expected Immunefi impact: Reflection/amplification abuse of nodes against third-party targets.
- Fast validation: Integration test asserting `newCalculatedRemotesEntryFromConfig` sends nothing toward an address that has no authenticated relationship.
