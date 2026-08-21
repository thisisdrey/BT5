# Q0002: Policy evaluated against wrong identity in NewRemoteAllowListFromConfig

## Question
Does `NewRemoteAllowListFromConfig` (allow_list.go) evaluate the rule set against the verified certificate of the sending session, or can the inner protocol number make it use stale, cached, or attacker-influenced identity data?

## Target
- File/function: `allow_list.go` -> `NewRemoteAllowListFromConfig` (declared at allow_list.go:59)
- Entrypoint: Tunnel packet whose inner IP header and payload are fully attacker-authored
- Attacker controls: the inner protocol number; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send tunnel traffic after forcing the identity source used by `NewRemoteAllowListFromConfig` to diverge from the session's verified certificate.
- Invariant to test: Every firewall decision uses the certificate cryptographically bound to the session that delivered the packet.
- Expected Immunefi impact: Firewall bypass: attacker traffic is authorized under another host's groups and reaches denied services.
- Fast validation: Unit test evaluating `NewRemoteAllowListFromConfig` with divergent session/cached identities, asserting the session's own certificate governs.
