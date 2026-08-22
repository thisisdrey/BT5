### Title
Unauthenticated `RecvError` packets can force remote tunnel teardown / state poisoning - ([File: outside.go])

### Summary
`fil_configure` in the Filsnap report allowed any unauthenticated caller (any dapp) to mutate shared snap state (network, RPC, unit config), poisoning behavior for other origins/users. The reachable analog in nebula is the `header.RecvError` control message path: `readOutsidePackets` dispatches it to `f.handleRecvError` before any cryptographic authentication (no Noise handshake, no certificate check) is performed, and the only anti-spoofing check inside `handleRecvError` is skipped whenever the target hostinfo has no established "current remote" yet, allowing an attacker who has never presented a CA-signed certificate to poison the victim's tunnel/handshake state.

### Finding Description
In `outside.go`, incoming UDP packets are parsed and dispatched by type before decryption/authentication: [1](#0-0) 

`header.RecvError` is handled immediately, with no Noise handshake state, no cert verification, and no HMAC/AEAD check on the packet — it only carries a 4-byte `RemoteIndex` in the cleartext header: [2](#0-1) 

The only defense against spoofing is:
```go
hr := hostinfo.GetRemote()
if hr.IsValid() && hr != addr {
    f.l.Info("Someone spoofing recv_errors?", ...)
    return
}
f.closeTunnel(hostinfo)
f.handshakeManager.DeleteHostInfo(hostinfo)
``` [3](#0-2) 

This check only fires if `hr.IsValid()` — i.e., if the hostinfo already has a known/pinned "current remote" address. If the tunnel's remote address is not yet pinned (e.g., before roaming/handshake settles, or for hosts reachable via multiple/changing remotes), or if the attacker can also spoof the UDP source `addr` itself (UDP has no built-in source authentication), the equality check provides no real protection. Because `RecvError` requires only guessing/observing a 32-bit `RemoteIndex` (not a cryptographic secret bound to a CA-issued identity), an attacker with no valid nebula certificate can send a single crafted packet to tear down an active tunnel and delete both the pending and main hostmap entries (`closeTunnel` + `DeleteHostInfo`), directly mutating shared connection state without ever authenticating.

This mirrors the `fil_configure` bug class: an entity that has not established trust (no CA-signed cert / no handshake) is able to remotely poison state (`ConnectionState`, hostmap entries) that legitimate, authenticated peers depend on.

### Impact Explanation
A successful spoofed `RecvError` forces `closeTunnel` and `DeleteHostInfo` on a live tunnel, tearing down an established, authenticated session between two legitimate CA-signed peers. This is a remote, unauthenticated denial-of-service / state-poisoning primitive: repeated injection can prevent tunnel stability ("flapping"), disrupt lighthouse/HostMap consistency, and force costly re-handshakes, all without the attacker ever presenting a valid certificate.

### Likelihood Explanation
Exploitation requires the attacker to know (or guess/observe) the 32-bit `RemoteIndex` value used by the target for a given tunnel and to be able to send a UDP packet that appears to originate from the peer's current remote address (or to hit the window where `hr` is not yet valid). This is nontrivial for a fully blind off-path attacker (32-bit index space) but is realistic for an on-path or partially-informed attacker (e.g., one who can observe some traffic, or targets a host whose remote address is not yet pinned, such as immediately after roam/handshake). The project's own changelog shows awareness of `recv_error` abuse risk (limiting `send_recv_error`/`accept_recv_error` behavior, and disabling sends outside the counter window), indicating this is a recognized but only partially mitigated risk area — consistent with the "partially addressed" status of the analogous Filsnap finding.

### Recommendation
- Require the `RecvError` handling to only be trusted when `hr.IsValid()` is true (i.e., reject state changes when the pinned remote is unknown, rather than allowing an unconditional `closeTunnel`).
- Consider dropping `RecvError`-triggered teardown entirely unless corroborated by additional signal (e.g., subsequent handshake attempt, or requiring the message to be authenticated/encrypted under the existing tunnel key rather than being a bare cleartext control message).
- Rate-limit/backoff `RecvError`-triggered `closeTunnel` calls per remote index to blunt blind/guessing attacks.
- Continue restricting via `listen.send_recv_error`/accept-error config, and document that `RecvError` is not an authenticated signal and should not be relied upon as a security boundary.

### Proof of Concept
1. Establish a legitimate tunnel between two CA-signed peers `A` and `B`.
2. As an attacker `E` with no valid nebula certificate, craft a raw UDP packet with `header.RecvError` type and `RemoteIndex` set to a value that matches one of `A`'s currently pending/main hostmap indexes (e.g., observed via traffic sniffing, or guessed during a narrow window such as right after roaming when `hr` is not yet `IsValid()`).
3. Send this packet to `A`'s listening UDP port, spoofing the source address to match (or targeting the window where the remote isn't pinned yet).
4. Observe in `A`'s logs/hostmap that `closeTunnel` and `DeleteHostInfo` are invoked (`outside.go:572-574`), tearing down the authenticated tunnel with `B` — achieved without `E` ever completing a Noise handshake or presenting a CA-signed certificate. [4](#0-3)

### Citations

**File:** outside.go (L19-24)
```go
const (
	minFwPacketLen = 4
)

var ErrOutOfWindow = errors.New("out of window packet")

```

**File:** outside.go (L76-84)
```go
	switch h.Type {
	case header.Handshake:
		f.handshakeManager.HandleIncoming(via, packet, h)
		return

	case header.RecvError:
		f.handleRecvError(via.UdpAddr, h)
		return
	}
```

**File:** outside.go (L541-561)
```go
func (f *Interface) handleRecvError(addr netip.AddrPort, h *header.H) {
	if !f.acceptRecvErrorConfig.ShouldRecvError(addr) {
		f.l.Debug("Recv error received, ignoring",
			"index", h.RemoteIndex,
			"udpAddr", addr,
		)
		return
	}

	if f.l.Enabled(context.Background(), slog.LevelDebug) {
		f.l.Debug("Recv error received",
			"index", h.RemoteIndex,
			"udpAddr", addr,
		)
	}

	hostinfo := f.hostMap.QueryReverseIndex(h.RemoteIndex)
	if hostinfo == nil {
		f.l.Debug("Did not find remote index in main hostmap", "remoteIndex", h.RemoteIndex)
		return
	}
```

**File:** outside.go (L562-575)
```go

	hr := hostinfo.GetRemote()
	if hr.IsValid() && hr != addr {
		f.l.Info("Someone spoofing recv_errors?",
			"addr", addr,
			"hostinfoRemote", hr,
		)
		return
	}

	f.closeTunnel(hostinfo)
	// We also delete it from pending hostmap to allow for fast reconnect.
	f.handshakeManager.DeleteHostInfo(hostinfo)
}
```
