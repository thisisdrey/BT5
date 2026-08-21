# Q0267: Write to tun without policy check in PermitInterface

## Question
Can a route with an attacker-influenced metric reach the tun write inside `PermitInterface` (wfp/wfp_windows.go) on a path that skips the firewall evaluation applied to the normal flow?

## Target
- File/function: `wfp/wfp_windows.go` -> `PermitInterface` (declared at wfp/wfp_windows.go:201)
- Entrypoint: Attacker-authored inner packet reaching the tun write path, or attacker-influenced route/MTU state
- Attacker controls: a route with an attacker-influenced metric; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Trace every caller reaching the write in `PermitInterface` and find one that does not evaluate the rule set first.
- Invariant to test: Every byte written to the tun device has passed firewall evaluation for the sending session.
- Expected Immunefi impact: Firewall bypass injecting attacker packets directly into the host's network stack.
- Fast validation: Integration test enumerating write callers and asserting a denied packet never appears on the tun device.
