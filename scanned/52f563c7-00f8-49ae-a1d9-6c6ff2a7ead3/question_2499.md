# Q2499: Address-family confusion in getAllRoutesFromConfig

## Question
Can an MTU-sized packet forcing a split write make `getAllRoutesFromConfig` (overlay/tun.go) treat an IPv6 packet as IPv4 (or an IPv4-mapped address as native), so the wrong header offsets or scope checks apply?

## Target
- File/function: `overlay/tun.go` -> `getAllRoutesFromConfig` (declared at overlay/tun.go:44)
- Entrypoint: Attacker-authored inner packet reaching the tun write path, or attacker-influenced route/MTU state
- Attacker controls: an MTU-sized packet forcing a split write; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send packets whose version nibble and actual content disagree.
- Invariant to test: Address family is determined once from the packet and consistently applied to all offset and scope logic.
- Expected Immunefi impact: Firewall/scope bypass through header misinterpretation.
- Fast validation: Differential test comparing family determination in `getAllRoutesFromConfig` against a reference parse.
