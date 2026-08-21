# Q2391: Group membership check in Interface.getOrHandshakeConsiderRouting

## Question
Can an attacker satisfy the group test in `Interface.getOrHandshakeConsiderRouting` (inside.go) via a conntrack-cached flow entry without actually holding every required group?

## Target
- File/function: `inside.go` -> `Interface.getOrHandshakeConsiderRouting` (declared at inside.go:146)
- Entrypoint: Tunnel packet whose inner IP header and payload are fully attacker-authored
- Attacker controls: a conntrack-cached flow entry; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Present group sets that are supersets, empty, duplicated, or differently ordered and observe the match.
- Invariant to test: A rule requiring groups matches only if the certificate contains all of them; empty never satisfies.
- Expected Immunefi impact: Firewall bypass granting access reserved to a privileged group.
- Fast validation: Table-driven unit test over group set variations asserting `Interface.getOrHandshakeConsiderRouting` requires full containment.
