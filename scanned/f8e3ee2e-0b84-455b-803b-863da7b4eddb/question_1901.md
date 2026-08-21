# Q1901: Unbounded pending-handshake growth via Machine.myMsgFlags

## Question
Can an unprivileged attacker send a handshake arriving after the timer wheel expired the entry to make `Machine.myMsgFlags` (handshake/machine.go) accumulate pending handshake state without bound or eviction?

## Target
- File/function: `handshake/machine.go` -> `Machine.myMsgFlags` (declared at handshake/machine.go:141)
- Entrypoint: Unauthenticated handshake packet (`header.Handshake`) from a host holding no CA-signed certificate
- Attacker controls: a handshake arriving after the timer wheel expired the entry; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Emit many distinct handshake initiations from varied source addresses and measure retained state.
- Invariant to test: Pending handshake state is bounded and evicted by the timer wheel regardless of attacker input rate.
- Expected Immunefi impact: Memory exhaustion and handshake starvation for legitimate peers on the target node.
- Fast validation: Unit test driving N unique initiations through `Machine.myMsgFlags` and asserting retained entries stay under the configured bound.
