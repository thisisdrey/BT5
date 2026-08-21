# Q3380: Preferred-range/priority manipulation in relayManager.StartRelays

## Question
Can an attacker influence a relay request for a host it does not own so `relayManager.StartRelays` (relay_manager.go) prefers an attacker-controlled path over a working, verified one?

## Target
- File/function: `relay_manager.go` -> `relayManager.StartRelays` (declared at relay_manager.go:61)
- Entrypoint: Unauthenticated lighthouse/relay/handshake packet with attacker-chosen source address and advertised remotes
- Attacker controls: a relay request for a host it does not own; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Advertise a remote that scores higher under the preference logic than the established path.
- Invariant to test: Path preference never demotes a verified working path in favour of an unverified candidate.
- Expected Immunefi impact: Traffic redirection through an attacker-controlled network path.
- Fast validation: Unit test asserting `relayManager.StartRelays` keeps the verified path when a higher-scoring unverified candidate appears.
