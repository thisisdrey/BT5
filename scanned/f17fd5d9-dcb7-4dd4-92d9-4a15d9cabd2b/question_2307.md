# Q2307: Lighthouse answer accepted without request in LightHouse.GetRelaysForMe

## Question
Does `LightHouse.GetRelaysForMe` (lighthouse.go) accept an oversized remote list that was never solicited, or that comes from a host that is not a configured lighthouse?

## Target
- File/function: `lighthouse.go` -> `LightHouse.GetRelaysForMe` (declared at lighthouse.go:167)
- Entrypoint: Unauthenticated lighthouse/relay/handshake packet with attacker-chosen source address and advertised remotes
- Attacker controls: an oversized remote list; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send an unsolicited reply naming a VPN address and see whether the remote list is updated.
- Invariant to test: Only replies matching an outstanding query from a configured lighthouse are processed.
- Expected Immunefi impact: Hostmap poisoning steering a victim's traffic to an attacker-chosen underlay address.
- Fast validation: Unit test feeding an unsolicited reply into `LightHouse.GetRelaysForMe` and asserting the remote list is untouched.
