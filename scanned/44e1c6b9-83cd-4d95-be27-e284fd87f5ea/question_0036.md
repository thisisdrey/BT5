# Q0036: Policy evaluated against wrong identity in NewFirewallFromConfig

## Question
Does `NewFirewallFromConfig` (firewall.go) evaluate the rule set against the verified certificate of the sending session, or can the destination port make it use stale, cached, or attacker-influenced identity data?

## Target
- File/function: `firewall.go` -> `NewFirewallFromConfig` (declared at firewall.go:195)
- Entrypoint: Tunnel packet whose inner IP header and payload are fully attacker-authored
- Attacker controls: the destination port; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send tunnel traffic after forcing the identity source used by `NewFirewallFromConfig` to diverge from the session's verified certificate.
- Invariant to test: Every firewall decision uses the certificate cryptographically bound to the session that delivered the packet.
- Expected Immunefi impact: Firewall bypass: attacker traffic is authorized under another host's groups and reaches denied services.
- Fast validation: Unit test evaluating `NewFirewallFromConfig` with divergent session/cached identities, asserting the session's own certificate governs.
