# Q2356: Private/loopback/self address filtering in newCalculatedRemotesEntryFromConfig

## Question
Does `newCalculatedRemotesEntryFromConfig` (calculated_remote.go) filter a HostQueryReply for an unrequested VPN address pointing at loopback, link-local, multicast, or the node's own address before using or storing it?

## Target
- File/function: `calculated_remote.go` -> `newCalculatedRemotesEntryFromConfig` (declared at calculated_remote.go:126)
- Entrypoint: Unauthenticated lighthouse/relay/handshake packet with attacker-chosen source address and advertised remotes
- Attacker controls: a HostQueryReply for an unrequested VPN address; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Advertise such addresses and check whether the node stores or dials them.
- Invariant to test: Non-routable, self, and reserved addresses are rejected before entering any remote list.
- Expected Immunefi impact: Self-connection loops, local-service reflection, or resource exhaustion on the target node.
- Fast validation: Table-driven unit test over reserved address classes asserting `newCalculatedRemotesEntryFromConfig` filters each.
