# Q2334: Third-party address advertisement in Punchy.reload

## Question
Can an attacker advertise a relay request for a host it does not own through `Punchy.reload` (punchy.go) to make a victim node send traffic to an unrelated third party?

## Target
- File/function: `punchy.go` -> `Punchy.reload` (declared at punchy.go:77)
- Entrypoint: Unauthenticated lighthouse/relay/handshake packet with attacker-chosen source address and advertised remotes
- Attacker controls: a relay request for a host it does not own; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Advertise a remote pointing at a chosen victim IP:port and observe the node's outbound traffic.
- Invariant to test: Advertised remotes are only ever used for the identity that owns them, and are filtered against configured ranges.
- Expected Immunefi impact: Traffic amplification/reflection using nodes as unwitting senders toward a third-party target.
- Fast validation: Integration test asserting `Punchy.reload` never emits packets to an address advertised for a foreign identity.
