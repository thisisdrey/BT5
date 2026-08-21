# Q2070: Group membership check in NewRemoteAllowListFromConfig

## Question
Can an attacker satisfy the group test in `NewRemoteAllowListFromConfig` (allow_list.go) via the sending certificate's CA name/SHA without actually holding every required group?

## Target
- File/function: `allow_list.go` -> `NewRemoteAllowListFromConfig` (declared at allow_list.go:59)
- Entrypoint: Tunnel packet whose inner IP header and payload are fully attacker-authored
- Attacker controls: the sending certificate's CA name/SHA; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Present group sets that are supersets, empty, duplicated, or differently ordered and observe the match.
- Invariant to test: A rule requiring groups matches only if the certificate contains all of them; empty never satisfies.
- Expected Immunefi impact: Firewall bypass granting access reserved to a privileged group.
- Fast validation: Table-driven unit test over group set variations asserting `NewRemoteAllowListFromConfig` requires full containment.
