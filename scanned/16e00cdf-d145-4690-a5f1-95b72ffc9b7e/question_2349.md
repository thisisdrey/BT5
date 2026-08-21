# Q2349: Route lookup manipulation in newSession

## Question
Can an attacker influence a zero-length inner payload so `newSession` (wfp/wfp_windows.go) selects a route or gateway other than the configured one?

## Target
- File/function: `wfp/wfp_windows.go` -> `newSession` (declared at wfp/wfp_windows.go:237)
- Entrypoint: Attacker-authored inner packet reaching the tun write path, or attacker-influenced route/MTU state
- Attacker controls: a zero-length inner payload; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send traffic crafted to shift the route selection inputs and observe the chosen next hop.
- Invariant to test: Route selection depends only on local configuration and the packet's destination, never on peer-supplied data.
- Expected Immunefi impact: Traffic redirection out an unintended interface or gateway.
- Fast validation: Unit test asserting `newSession` returns the configured route for every attacker-shaped input.
