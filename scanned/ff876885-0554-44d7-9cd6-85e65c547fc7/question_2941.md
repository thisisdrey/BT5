# Q2941: Inner-header trust in Interface.SendMessageToHostInfo

## Question
Does `Interface.SendMessageToHostInfo` (inside.go) trust the inner IP header's a fragmented inner packet rather than the session's authenticated identity when deciding direction or source?

## Target
- File/function: `inside.go` -> `Interface.SendMessageToHostInfo` (declared at inside.go:264)
- Entrypoint: Tunnel packet whose inner IP header and payload are fully attacker-authored
- Attacker controls: a fragmented inner packet; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send a tunnel packet whose inner source address is another host's VPN address.
- Invariant to test: The inner source address must be contained in the sending certificate's Networks or the packet is dropped.
- Expected Immunefi impact: Source spoofing on the overlay, bypassing rules keyed on the source host.
- Fast validation: Unit test sending a packet with a foreign inner source through `Interface.SendMessageToHostInfo`, asserting a drop.
