# Q0040: Policy evaluated against wrong identity in Packet.Copy

## Question
Does `Packet.Copy` (firewall/packet.go) evaluate the rule set against the verified certificate of the sending session, or can the inner source VPN address make it use stale, cached, or attacker-influenced identity data?

## Target
- File/function: `firewall/packet.go` -> `Packet.Copy` (declared at firewall/packet.go:34)
- Entrypoint: Tunnel packet whose inner IP header and payload are fully attacker-authored
- Attacker controls: the inner source VPN address; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send tunnel traffic after forcing the identity source used by `Packet.Copy` to diverge from the session's verified certificate.
- Invariant to test: Every firewall decision uses the certificate cryptographically bound to the session that delivered the packet.
- Expected Immunefi impact: Firewall bypass: attacker traffic is authorized under another host's groups and reaches denied services.
- Fast validation: Unit test evaluating `Packet.Copy` with divergent session/cached identities, asserting the session's own certificate governs.
