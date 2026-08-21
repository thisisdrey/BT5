# Q2764: Inner-header trust in Interface.rejectOutside

## Question
Does `Interface.rejectOutside` (inside.go) trust the inner IP header's a localCIDR-restricted rule rather than the session's authenticated identity when deciding direction or source?

## Target
- File/function: `inside.go` -> `Interface.rejectOutside` (declared at inside.go:105)
- Entrypoint: Tunnel packet whose inner IP header and payload are fully attacker-authored
- Attacker controls: a localCIDR-restricted rule; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send a tunnel packet whose inner source address is another host's VPN address.
- Invariant to test: The inner source address must be contained in the sending certificate's Networks or the packet is dropped.
- Expected Immunefi impact: Source spoofing on the overlay, bypassing rules keyed on the source host.
- Fast validation: Unit test sending a packet with a foreign inner source through `Interface.rejectOutside`, asserting a drop.
