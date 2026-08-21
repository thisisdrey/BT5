# Q2506: Hostmap keyed on attacker-chosen value in relayManager.StartRelays

## Question
Does `relayManager.StartRelays` (relay_manager.go) key hostmap or index structures on a spoofed UDP source address that an attacker can choose, enabling collision or eviction of a live entry?

## Target
- File/function: `relay_manager.go` -> `relayManager.StartRelays` (declared at relay_manager.go:61)
- Entrypoint: Unauthenticated lighthouse/relay/handshake packet with attacker-chosen source address and advertised remotes
- Attacker controls: a spoofed UDP source address; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Choose the keying value to collide with a live peer and observe the map after insertion.
- Invariant to test: Hostmap keys derive from verified identity, and insertion never displaces an authenticated live entry.
- Expected Immunefi impact: Session takeover or denial of service against a chosen overlay host.
- Fast validation: Unit test inserting a colliding key through `relayManager.StartRelays` and asserting the authenticated entry survives.
