# Q1526: Write to tun without policy check in UserDevice.Close

## Question
Can a multicast/broadcast inner destination reach the tun write inside `UserDevice.Close` (overlay/user.go) on a path that skips the firewall evaluation applied to the normal flow?

## Target
- File/function: `overlay/user.go` -> `UserDevice.Close` (declared at overlay/user.go:67)
- Entrypoint: Attacker-authored inner packet reaching the tun write path, or attacker-influenced route/MTU state
- Attacker controls: a multicast/broadcast inner destination; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Trace every caller reaching the write in `UserDevice.Close` and find one that does not evaluate the rule set first.
- Invariant to test: Every byte written to the tun device has passed firewall evaluation for the sending session.
- Expected Immunefi impact: Firewall bypass injecting attacker packets directly into the host's network stack.
- Fast validation: Integration test enumerating write callers and asserting a denied packet never appears on the tun device.
