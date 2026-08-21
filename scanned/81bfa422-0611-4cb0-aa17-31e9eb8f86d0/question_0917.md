# Q0917: Unknown-value default-allow in NewListener

## Question
Does `NewListener` (udp/udp_android.go) fall through to a permissive or default branch when an attacker supplies header.RemoteIndex, instead of failing closed and dropping the packet?

## Target
- File/function: `udp/udp_android.go` -> `NewListener` (declared at udp/udp_android.go:16)
- Entrypoint: Unauthenticated UDP datagram delivered to the listener and dispatched through `Interface.readOutsidePackets`
- Attacker controls: header.RemoteIndex; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Enumerate values outside the known enum/range and observe which branch of `NewListener` executes.
- Invariant to test: Unknown or reserved wire values are rejected, never mapped onto a default handler.
- Expected Immunefi impact: Authentication or firewall bypass reached by steering an unauthenticated packet into a handler it should never reach.
- Fast validation: Table-driven unit test over all 256 values of the field, asserting `NewListener` returns an error for every value not explicitly supported.
