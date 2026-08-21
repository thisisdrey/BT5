# Q3044: Private/loopback/self address filtering in relayManager.reload

## Question
Does `relayManager.reload` (relay_manager.go) filter a preferred-ranges entry pointing at loopback, link-local, multicast, or the node's own address before using or storing it?

## Target
- File/function: `relay_manager.go` -> `relayManager.reload` (declared at relay_manager.go:40)
- Entrypoint: Unauthenticated lighthouse/relay/handshake packet with attacker-chosen source address and advertised remotes
- Attacker controls: a preferred-ranges entry; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Advertise such addresses and check whether the node stores or dials them.
- Invariant to test: Non-routable, self, and reserved addresses are rejected before entering any remote list.
- Expected Immunefi impact: Self-connection loops, local-service reflection, or resource exhaustion on the target node.
- Fast validation: Table-driven unit test over reserved address classes asserting `relayManager.reload` filters each.
