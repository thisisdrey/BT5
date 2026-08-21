# Q2500: Address-family confusion in findRemovedRoutes

## Question
Can a packet destined at the tun device's own address make `findRemovedRoutes` (overlay/tun.go) treat an IPv6 packet as IPv4 (or an IPv4-mapped address as native), so the wrong header offsets or scope checks apply?

## Target
- File/function: `overlay/tun.go` -> `findRemovedRoutes` (declared at overlay/tun.go:65)
- Entrypoint: Attacker-authored inner packet reaching the tun write path, or attacker-influenced route/MTU state
- Attacker controls: a packet destined at the tun device's own address; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send packets whose version nibble and actual content disagree.
- Invariant to test: Address family is determined once from the packet and consistently applied to all offset and scope logic.
- Expected Immunefi impact: Firewall/scope bypass through header misinterpretation.
- Fast validation: Differential test comparing family determination in `findRemovedRoutes` against a reference parse.
