# Q3037: Lighthouse answer accepted without request in LightHouse.StartUpdateWorker

## Question
Does `LightHouse.StartUpdateWorker` (lighthouse.go) accept an advertised private/loopback remote that was never solicited, or that comes from a host that is not a configured lighthouse?

## Target
- File/function: `lighthouse.go` -> `LightHouse.StartUpdateWorker` (declared at lighthouse.go:877)
- Entrypoint: Unauthenticated lighthouse/relay/handshake packet with attacker-chosen source address and advertised remotes
- Attacker controls: an advertised private/loopback remote; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send an unsolicited reply naming a VPN address and see whether the remote list is updated.
- Invariant to test: Only replies matching an outstanding query from a configured lighthouse are processed.
- Expected Immunefi impact: Hostmap poisoning steering a victim's traffic to an attacker-chosen underlay address.
- Fast validation: Unit test feeding an unsolicited reply into `LightHouse.StartUpdateWorker` and asserting the remote list is untouched.
