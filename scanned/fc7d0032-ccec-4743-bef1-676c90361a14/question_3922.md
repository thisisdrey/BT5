# Q3922: Group membership check in newFirewallTable

## Question
Can an attacker satisfy the group test in `newFirewallTable` (firewall.go) via the sending certificate's groups without actually holding every required group?

## Target
- File/function: `firewall.go` -> `newFirewallTable` (declared at firewall.go:97)
- Entrypoint: Tunnel packet whose inner IP header and payload are fully attacker-authored
- Attacker controls: the sending certificate's groups; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Present group sets that are supersets, empty, duplicated, or differently ordered and observe the match.
- Invariant to test: A rule requiring groups matches only if the certificate contains all of them; empty never satisfies.
- Expected Immunefi impact: Firewall bypass granting access reserved to a privileged group.
- Fast validation: Table-driven unit test over group set variations asserting `newFirewallTable` requires full containment.
