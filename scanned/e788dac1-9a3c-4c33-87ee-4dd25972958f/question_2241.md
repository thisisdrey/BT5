# Q2241: Preferred-range/priority manipulation in hashPacket

## Question
Can an attacker influence a preferred-ranges entry so `hashPacket` (routing/balance.go) prefers an attacker-controlled path over a working, verified one?

## Target
- File/function: `routing/balance.go` -> `hashPacket` (declared at routing/balance.go:14)
- Entrypoint: Unauthenticated lighthouse/relay/handshake packet with attacker-chosen source address and advertised remotes
- Attacker controls: a preferred-ranges entry; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Advertise a remote that scores higher under the preference logic than the established path.
- Invariant to test: Path preference never demotes a verified working path in favour of an unverified candidate.
- Expected Immunefi impact: Traffic redirection through an attacker-controlled network path.
- Fast validation: Unit test asserting `hashPacket` keeps the verified path when a higher-scoring unverified candidate appears.
