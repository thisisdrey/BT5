# Q1091: Policy evaluated against wrong identity in convertRule

## Question
Does `convertRule` (firewall.go) evaluate the rule set against the verified certificate of the sending session, or can the sending certificate's CA name/SHA make it use stale, cached, or attacker-influenced identity data?

## Target
- File/function: `firewall.go` -> `convertRule` (declared at firewall.go:944)
- Entrypoint: Tunnel packet whose inner IP header and payload are fully attacker-authored
- Attacker controls: the sending certificate's CA name/SHA; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send tunnel traffic after forcing the identity source used by `convertRule` to diverge from the session's verified certificate.
- Invariant to test: Every firewall decision uses the certificate cryptographically bound to the session that delivered the packet.
- Expected Immunefi impact: Firewall bypass: attacker traffic is authorized under another host's groups and reaches denied services.
- Fast validation: Unit test evaluating `convertRule` with divergent session/cached identities, asserting the session's own certificate governs.
