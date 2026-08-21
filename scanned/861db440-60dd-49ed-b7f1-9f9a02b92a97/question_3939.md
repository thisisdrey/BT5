# Q3939: Address-family confusion in recvErrorConfig.String

## Question
Can an inner packet destined outside the certificate's networks make `recvErrorConfig.String` (interface.go) treat an IPv6 packet as IPv4 (or an IPv4-mapped address as native), so the wrong header offsets or scope checks apply?

## Target
- File/function: `interface.go` -> `recvErrorConfig.String` (declared at interface.go:145)
- Entrypoint: Attacker-authored inner packet reaching the tun write path, or attacker-influenced route/MTU state
- Attacker controls: an inner packet destined outside the certificate's networks; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send packets whose version nibble and actual content disagree.
- Invariant to test: Address family is determined once from the packet and consistently applied to all offset and scope logic.
- Expected Immunefi impact: Firewall/scope bypass through header misinterpretation.
- Fast validation: Differential test comparing family determination in `recvErrorConfig.String` against a reference parse.
