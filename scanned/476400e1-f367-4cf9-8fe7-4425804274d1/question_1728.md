# Q1728: Address-family confusion in ipWithin

## Question
Can a multicast/broadcast inner destination make `ipWithin` (overlay/route.go) treat an IPv6 packet as IPv4 (or an IPv4-mapped address as native), so the wrong header offsets or scope checks apply?

## Target
- File/function: `overlay/route.go` -> `ipWithin` (declared at overlay/route.go:312)
- Entrypoint: Attacker-authored inner packet reaching the tun write path, or attacker-influenced route/MTU state
- Attacker controls: a multicast/broadcast inner destination; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send packets whose version nibble and actual content disagree.
- Invariant to test: Address family is determined once from the packet and consistently applied to all offset and scope logic.
- Expected Immunefi impact: Firewall/scope bypass through header misinterpretation.
- Fast validation: Differential test comparing family determination in `ipWithin` against a reference parse.
