# Q2542: Preferred-range/priority manipulation in calculatedRemote.ApplyV6

## Question
Can an attacker influence an advertised remote pointing at a third party so `calculatedRemote.ApplyV6` (calculated_remote.go) prefers an attacker-controlled path over a working, verified one?

## Target
- File/function: `calculated_remote.go` -> `calculatedRemote.ApplyV6` (declared at calculated_remote.go:59)
- Entrypoint: Unauthenticated lighthouse/relay/handshake packet with attacker-chosen source address and advertised remotes
- Attacker controls: an advertised remote pointing at a third party; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Advertise a remote that scores higher under the preference logic than the established path.
- Invariant to test: Path preference never demotes a verified working path in favour of an unverified candidate.
- Expected Immunefi impact: Traffic redirection through an attacker-controlled network path.
- Fast validation: Unit test asserting `calculatedRemote.ApplyV6` keeps the verified path when a higher-scoring unverified candidate appears.
