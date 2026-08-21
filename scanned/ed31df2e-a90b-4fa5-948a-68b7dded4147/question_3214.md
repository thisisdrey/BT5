# Q3214: Private/loopback/self address filtering in relayManager.HandleControlMsg

## Question
Does `relayManager.HandleControlMsg` (relay_manager.go) filter a HostUpdateNotification for another host's address pointing at loopback, link-local, multicast, or the node's own address before using or storing it?

## Target
- File/function: `relay_manager.go` -> `relayManager.HandleControlMsg` (declared at relay_manager.go:298)
- Entrypoint: Unauthenticated lighthouse/relay/handshake packet with attacker-chosen source address and advertised remotes
- Attacker controls: a HostUpdateNotification for another host's address; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Advertise such addresses and check whether the node stores or dials them.
- Invariant to test: Non-routable, self, and reserved addresses are rejected before entering any remote list.
- Expected Immunefi impact: Self-connection loops, local-service reflection, or resource exhaustion on the target node.
- Fast validation: Table-driven unit test over reserved address classes asserting `relayManager.HandleControlMsg` filters each.
