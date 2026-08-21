# Q2507: Hostmap keyed on attacker-chosen value in AddRelay

## Question
Does `AddRelay` (relay_manager.go) key hostmap or index structures on an advertised private/loopback remote that an attacker can choose, enabling collision or eviction of a live entry?

## Target
- File/function: `relay_manager.go` -> `AddRelay` (declared at relay_manager.go:229)
- Entrypoint: Unauthenticated lighthouse/relay/handshake packet with attacker-chosen source address and advertised remotes
- Attacker controls: an advertised private/loopback remote; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Choose the keying value to collide with a live peer and observe the map after insertion.
- Invariant to test: Hostmap keys derive from verified identity, and insertion never displaces an authenticated live entry.
- Expected Immunefi impact: Session takeover or denial of service against a chosen overlay host.
- Fast validation: Unit test inserting a colliding key through `AddRelay` and asserting the authenticated entry survives.
