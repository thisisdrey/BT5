# Q1772: Hostmap keyed on attacker-chosen value in newCalculatedRemote

## Question
Does `newCalculatedRemote` (calculated_remote.go) key hostmap or index structures on a punch notification naming an arbitrary target that an attacker can choose, enabling collision or eviction of a live entry?

## Target
- File/function: `calculated_remote.go` -> `newCalculatedRemote` (declared at calculated_remote.go:24)
- Entrypoint: Unauthenticated lighthouse/relay/handshake packet with attacker-chosen source address and advertised remotes
- Attacker controls: a punch notification naming an arbitrary target; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Choose the keying value to collide with a live peer and observe the map after insertion.
- Invariant to test: Hostmap keys derive from verified identity, and insertion never displaces an authenticated live entry.
- Expected Immunefi impact: Session takeover or denial of service against a chosen overlay host.
- Fast validation: Unit test inserting a colliding key through `newCalculatedRemote` and asserting the authenticated entry survives.
