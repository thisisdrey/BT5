# Q2259: Group membership check in LocalAllowList.Allow

## Question
Can an attacker satisfy the group test in `LocalAllowList.Allow` (allow_list.go) via the inner protocol number without actually holding every required group?

## Target
- File/function: `allow_list.go` -> `LocalAllowList.Allow` (declared at allow_list.go:248)
- Entrypoint: Tunnel packet whose inner IP header and payload are fully attacker-authored
- Attacker controls: the inner protocol number; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Present group sets that are supersets, empty, duplicated, or differently ordered and observe the match.
- Invariant to test: A rule requiring groups matches only if the certificate contains all of them; empty never satisfies.
- Expected Immunefi impact: Firewall bypass granting access reserved to a privileged group.
- Fast validation: Table-driven unit test over group set variations asserting `LocalAllowList.Allow` requires full containment.
