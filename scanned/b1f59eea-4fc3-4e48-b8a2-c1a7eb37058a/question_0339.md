# Q0339: Unauthenticated remote update via LightHouse.getCalculatedRemotes

## Question
Can an unprivileged attacker send an oversized remote list so `LightHouse.getCalculatedRemotes` (lighthouse.go) changes the underlay address recorded for an established peer?

## Target
- File/function: `lighthouse.go` -> `LightHouse.getCalculatedRemotes` (declared at lighthouse.go:171)
- Entrypoint: Unauthenticated lighthouse/relay/handshake packet with attacker-chosen source address and advertised remotes
- Attacker controls: an oversized remote list; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send the address-bearing packet type for a live session from an unrelated source address.
- Invariant to test: A peer's recorded remote changes only after authenticated traffic from that peer proves the new path.
- Expected Immunefi impact: Traffic redirection/blackholing of an established tunnel by an off-path attacker.
- Fast validation: Integration test injecting an unauthenticated update through `LightHouse.getCalculatedRemotes` and asserting the peer's remote is unchanged.
