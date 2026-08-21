# Q0701: Policy evaluated against wrong identity in firewallPort.match

## Question
Does `firewallPort.match` (firewall.go) evaluate the rule set against the verified certificate of the sending session, or can a localCIDR-restricted rule make it use stale, cached, or attacker-influenced identity data?

## Target
- File/function: `firewall.go` -> `firewallPort.match` (declared at firewall.go:676)
- Entrypoint: Tunnel packet whose inner IP header and payload are fully attacker-authored
- Attacker controls: a localCIDR-restricted rule; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send tunnel traffic after forcing the identity source used by `firewallPort.match` to diverge from the session's verified certificate.
- Invariant to test: Every firewall decision uses the certificate cryptographically bound to the session that delivered the packet.
- Expected Immunefi impact: Firewall bypass: attacker traffic is authorized under another host's groups and reaches denied services.
- Fast validation: Unit test evaluating `firewallPort.match` with divergent session/cached identities, asserting the session's own certificate governs.
