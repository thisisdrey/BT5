# Q3299: Preferred-range/priority manipulation in relayManager.GetAmRelay

## Question
Can an attacker influence an advertised private/loopback remote so `relayManager.GetAmRelay` (relay_manager.go) prefers an attacker-controlled path over a working, verified one?

## Target
- File/function: `relay_manager.go` -> `relayManager.GetAmRelay` (declared at relay_manager.go:49)
- Entrypoint: Unauthenticated lighthouse/relay/handshake packet with attacker-chosen source address and advertised remotes
- Attacker controls: an advertised private/loopback remote; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Advertise a remote that scores higher under the preference logic than the established path.
- Invariant to test: Path preference never demotes a verified working path in favour of an unverified candidate.
- Expected Immunefi impact: Traffic redirection through an attacker-controlled network path.
- Fast validation: Unit test asserting `relayManager.GetAmRelay` keeps the verified path when a higher-scoring unverified candidate appears.
