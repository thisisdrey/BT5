# Q2595: Address-family confusion in getBroadcast

## Question
Can an inner packet destined outside the certificate's networks make `getBroadcast` (overlay/tun.go) treat an IPv6 packet as IPv4 (or an IPv4-mapped address as native), so the wrong header offsets or scope checks apply?

## Target
- File/function: `overlay/tun.go` -> `getBroadcast` (declared at overlay/tun.go:109)
- Entrypoint: Attacker-authored inner packet reaching the tun write path, or attacker-influenced route/MTU state
- Attacker controls: an inner packet destined outside the certificate's networks; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send packets whose version nibble and actual content disagree.
- Invariant to test: Address family is determined once from the packet and consistently applied to all offset and scope logic.
- Expected Immunefi impact: Firewall/scope bypass through header misinterpretation.
- Fast validation: Differential test comparing family determination in `getBroadcast` against a reference parse.
