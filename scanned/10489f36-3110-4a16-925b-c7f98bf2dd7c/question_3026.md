# Q3026: Inner-header trust in Interface.dropExhausted

## Question
Does `Interface.dropExhausted` (inside.go) trust the inner IP header's an unsafe_routes destination rather than the session's authenticated identity when deciding direction or source?

## Target
- File/function: `inside.go` -> `Interface.dropExhausted` (declared at inside.go:279)
- Entrypoint: Tunnel packet whose inner IP header and payload are fully attacker-authored
- Attacker controls: an unsafe_routes destination; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send a tunnel packet whose inner source address is another host's VPN address.
- Invariant to test: The inner source address must be contained in the sending certificate's Networks or the packet is dropped.
- Expected Immunefi impact: Source spoofing on the overlay, bypassing rules keyed on the source host.
- Fast validation: Unit test sending a packet with a foreign inner source through `Interface.dropExhausted`, asserting a drop.
