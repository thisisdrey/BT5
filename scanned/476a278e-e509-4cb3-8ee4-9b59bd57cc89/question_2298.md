# Q2298: Group membership check in Interface.Handshake

## Question
Can an attacker satisfy the group test in `Interface.Handshake` (inside.go) via a localCIDR-restricted rule without actually holding every required group?

## Target
- File/function: `inside.go` -> `Interface.Handshake` (declared at inside.go:130)
- Entrypoint: Tunnel packet whose inner IP header and payload are fully attacker-authored
- Attacker controls: a localCIDR-restricted rule; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Present group sets that are supersets, empty, duplicated, or differently ordered and observe the match.
- Invariant to test: A rule requiring groups matches only if the certificate contains all of them; empty never satisfies.
- Expected Immunefi impact: Firewall bypass granting access reserved to a privileged group.
- Fast validation: Table-driven unit test over group set variations asserting `Interface.Handshake` requires full containment.
