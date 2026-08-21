# Q2355: Private/loopback/self address filtering in newCalculatedRemotesListFromConfig

## Question
Does `newCalculatedRemotesListFromConfig` (calculated_remote.go) filter a relay request for a host it does not own pointing at loopback, link-local, multicast, or the node's own address before using or storing it?

## Target
- File/function: `calculated_remote.go` -> `newCalculatedRemotesListFromConfig` (declared at calculated_remote.go:108)
- Entrypoint: Unauthenticated lighthouse/relay/handshake packet with attacker-chosen source address and advertised remotes
- Attacker controls: a relay request for a host it does not own; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Advertise such addresses and check whether the node stores or dials them.
- Invariant to test: Non-routable, self, and reserved addresses are rejected before entering any remote list.
- Expected Immunefi impact: Self-connection loops, local-service reflection, or resource exhaustion on the target node.
- Fast validation: Table-driven unit test over reserved address classes asserting `newCalculatedRemotesListFromConfig` filters each.
