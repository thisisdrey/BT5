# Q2580: Group membership check in Interface.SendVia

## Question
Can an attacker satisfy the group test in `Interface.SendVia` (inside.go) via an unsafe_routes destination without actually holding every required group?

## Target
- File/function: `inside.go` -> `Interface.SendVia` (declared at inside.go:294)
- Entrypoint: Tunnel packet whose inner IP header and payload are fully attacker-authored
- Attacker controls: an unsafe_routes destination; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Present group sets that are supersets, empty, duplicated, or differently ordered and observe the match.
- Invariant to test: A rule requiring groups matches only if the certificate contains all of them; empty never satisfies.
- Expected Immunefi impact: Firewall bypass granting access reserved to a privileged group.
- Fast validation: Table-driven unit test over group set variations asserting `Interface.SendVia` requires full containment.
