# Q1529: Lighthouse answer accepted without request in Gateway.String

## Question
Does `Gateway.String` (routing/gateway.go) accept a HostUpdateNotification for another host's address that was never solicited, or that comes from a host that is not a configured lighthouse?

## Target
- File/function: `routing/gateway.go` -> `Gateway.String` (declared at routing/gateway.go:44)
- Entrypoint: Unauthenticated lighthouse/relay/handshake packet with attacker-chosen source address and advertised remotes
- Attacker controls: a HostUpdateNotification for another host's address; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send an unsolicited reply naming a VPN address and see whether the remote list is updated.
- Invariant to test: Only replies matching an outstanding query from a configured lighthouse are processed.
- Expected Immunefi impact: Hostmap poisoning steering a victim's traffic to an attacker-chosen underlay address.
- Fast validation: Unit test feeding an unsolicited reply into `Gateway.String` and asserting the remote list is untouched.
