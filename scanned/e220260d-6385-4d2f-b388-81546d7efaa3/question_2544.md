# Q2544: Preferred-range/priority manipulation in newCalculatedRemotesListFromConfig

## Question
Can an attacker influence a HostQueryReply for an unrequested VPN address so `newCalculatedRemotesListFromConfig` (calculated_remote.go) prefers an attacker-controlled path over a working, verified one?

## Target
- File/function: `calculated_remote.go` -> `newCalculatedRemotesListFromConfig` (declared at calculated_remote.go:108)
- Entrypoint: Unauthenticated lighthouse/relay/handshake packet with attacker-chosen source address and advertised remotes
- Attacker controls: a HostQueryReply for an unrequested VPN address; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Advertise a remote that scores higher under the preference logic than the established path.
- Invariant to test: Path preference never demotes a verified working path in favour of an unverified candidate.
- Expected Immunefi impact: Traffic redirection through an attacker-controlled network path.
- Fast validation: Unit test asserting `newCalculatedRemotesListFromConfig` keeps the verified path when a higher-scoring unverified candidate appears.
