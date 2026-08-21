# Q2449: Preferred-range/priority manipulation in calculatedRemote.String

## Question
Can an attacker influence a spoofed UDP source address so `calculatedRemote.String` (calculated_remote.go) prefers an attacker-controlled path over a working, verified one?

## Target
- File/function: `calculated_remote.go` -> `calculatedRemote.String` (declared at calculated_remote.go:41)
- Entrypoint: Unauthenticated lighthouse/relay/handshake packet with attacker-chosen source address and advertised remotes
- Attacker controls: a spoofed UDP source address; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Advertise a remote that scores higher under the preference logic than the established path.
- Invariant to test: Path preference never demotes a verified working path in favour of an unverified candidate.
- Expected Immunefi impact: Traffic redirection through an attacker-controlled network path.
- Fast validation: Unit test asserting `calculatedRemote.String` keeps the verified path when a higher-scoring unverified candidate appears.
