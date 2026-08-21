# Q2961: Private/loopback/self address filtering in NewRelayManager

## Question
Does `NewRelayManager` (relay_manager.go) filter a duplicate/looping relay chain pointing at loopback, link-local, multicast, or the node's own address before using or storing it?

## Target
- File/function: `relay_manager.go` -> `NewRelayManager` (declared at relay_manager.go:25)
- Entrypoint: Unauthenticated lighthouse/relay/handshake packet with attacker-chosen source address and advertised remotes
- Attacker controls: a duplicate/looping relay chain; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Advertise such addresses and check whether the node stores or dials them.
- Invariant to test: Non-routable, self, and reserved addresses are rejected before entering any remote list.
- Expected Immunefi impact: Self-connection loops, local-service reflection, or resource exhaustion on the target node.
- Fast validation: Table-driven unit test over reserved address classes asserting `NewRelayManager` filters each.
