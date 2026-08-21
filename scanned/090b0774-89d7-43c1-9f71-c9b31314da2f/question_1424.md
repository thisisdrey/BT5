# Q1424: Write to tun without policy check in NativeTun.LUID

## Question
Can a packet destined at the tun device's own address reach the tun write inside `NativeTun.LUID` (wintun/tun.go) on a path that skips the firewall evaluation applied to the normal flow?

## Target
- File/function: `wintun/tun.go` -> `NativeTun.LUID` (declared at wintun/tun.go:182)
- Entrypoint: Attacker-authored inner packet reaching the tun write path, or attacker-influenced route/MTU state
- Attacker controls: a packet destined at the tun device's own address; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Trace every caller reaching the write in `NativeTun.LUID` and find one that does not evaluate the rule set first.
- Invariant to test: Every byte written to the tun device has passed firewall evaluation for the sending session.
- Expected Immunefi impact: Firewall bypass injecting attacker packets directly into the host's network stack.
- Fast validation: Integration test enumerating write callers and asserting a denied packet never appears on the tun device.
