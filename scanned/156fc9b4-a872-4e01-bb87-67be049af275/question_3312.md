# Q3312: Route lookup manipulation in UserDevice.SupportsMultiqueue

## Question
Can an attacker influence a route with an attacker-influenced metric so `UserDevice.SupportsMultiqueue` (overlay/user.go) selects a route or gateway other than the configured one?

## Target
- File/function: `overlay/user.go` -> `UserDevice.SupportsMultiqueue` (declared at overlay/user.go:49)
- Entrypoint: Attacker-authored inner packet reaching the tun write path, or attacker-influenced route/MTU state
- Attacker controls: a route with an attacker-influenced metric; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send traffic crafted to shift the route selection inputs and observe the chosen next hop.
- Invariant to test: Route selection depends only on local configuration and the packet's destination, never on peer-supplied data.
- Expected Immunefi impact: Traffic redirection out an unintended interface or gateway.
- Fast validation: Unit test asserting `UserDevice.SupportsMultiqueue` returns the configured route for every attacker-shaped input.
