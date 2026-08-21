# Q1381: Panic on device error path in makeRouteTree

## Question
Can attacker-paced an inner packet destined outside the certificate's networks drive `makeRouteTree` (overlay/route.go) into a device error path that panics or leaves the interface unusable?

## Target
- File/function: `overlay/route.go` -> `makeRouteTree` (declared at overlay/route.go:51)
- Entrypoint: Attacker-authored inner packet reaching the tun write path, or attacker-influenced route/MTU state
- Attacker controls: an inner packet destined outside the certificate's networks; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Force write failures under attacker-controlled load and observe recovery.
- Invariant to test: Device errors are handled without panic and the interface recovers or reports cleanly.
- Expected Immunefi impact: Persistent node outage triggered by remote traffic.
- Fast validation: Fault-injection test returning errors from the device write in `makeRouteTree`, asserting no panic and continued operation.
