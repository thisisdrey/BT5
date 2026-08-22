### Title
Unauthenticated `RecvError` packets allow a certificate-less attacker to tear down established tunnels between legitimate peers - ([File: outside.go])

### Summary
Nebula's `RecvError` handler tears down an existing, authenticated tunnel based solely on an unauthenticated UDP packet whose only "proof" is a guessable 32-bit index and a source `ip:port` comparison — both of which are spoofable/observable by an attacker who holds no CA-signed certificate at all.

### Finding Description
`handleRecvError` in `outside.go` processes an inbound `header.RecvError` message before any AEAD/cert-based authentication has occurred for that packet: [1](#0-0) 

The function:
1. Checks a config-driven `acceptRecvErrorConfig.ShouldRecvError(addr)` gate (source-address based, not cryptographic).
2. Looks up `hostinfo := f.hostMap.QueryReverseIndex(h.RemoteIndex)` — `RemoteIndex` is a plaintext 32-bit header field, unauthenticated and unencrypted, present on every packet on the wire.
3. Compares `hostinfo.GetRemote()` to the packet's source `addr`. Since this is UDP, source address is trivially spoofable, and the comparison provides no cryptographic guarantee that the sender is the actual tunnel peer.
4. If the check passes, it unconditionally calls `f.closeTunnel(hostinfo)` and `f.handshakeManager.DeleteHostInfo(hostinfo)` — mutating a *victim's* already-established, cert-authenticated tunnel state without the attacker ever presenting a valid certificate or completing the Noise handshake.

This mirrors the underlying bug class in the referenced report: a function that mutates another party's protected state (`lockedToken.unlockTime` there; the tunnel/hostinfo lifecycle here) is reachable by a party with no legitimate standing (no token stake there; no CA-signed cert here), gated only by weak, cheaply-forgeable checks (`_quantity==0` there; spoofable UDP source + guessable index here). Compare this to the legitimately gated `header.CloseTunnel` path, which is only processed after successful AEAD decryption inside `readOutsidePackets` — i.e., it *does* require possession of the negotiated symmetric key derived from a cert-authenticated handshake: [2](#0-1) [3](#0-2) 

`RecvError`, in contrast, is processed on the raw, unauthenticated packet path, so it bypasses the cert/AEAD gate that every other tunnel-affecting operation goes through.

### Impact Explanation
An attacker with no CA-signed certificate can forge `RecvError` packets to force `closeTunnel` + `DeleteHostInfo` on a victim's live, fully-authenticated tunnel with a legitimate peer. Repeated forging (trivial and cheap, requiring only a correctly-guessed/observed `RemoteIndex` and a spoofed source `ip:port`) can be used to continuously grief a victim into re-handshaking, denying stable connectivity — the same "repeatable, cost-free state-mutation" pattern as the `lockOnBehalf` finding, but here the mutated state is tunnel/session liveness rather than a token unlock timer. This is a remote state-poisoning / availability-disruption primitive against fully authenticated sessions, triggered by a principal that never proved possession of a CA-issued identity.

### Likelihood Explanation
The `RemoteIndex` is a 32-bit value; while `generateIndex` presumably randomizes it, it is also transmitted in the clear on every packet, so any attacker who can observe traffic on the path (or who is targeting a host whose index leaked via a prior packet capture) can obtain it without needing a valid cert. UDP source spoofing (or being positioned to send from the real peer's apparent vantage, e.g. NAT/on-path attacker) further lowers the bar. The exact strength of `acceptRecvErrorConfig.ShouldRecvError` (config in `interface.go`) was not fully reviewed in this pass — I could not confirm whether default configuration meaningfully restricts acceptance beyond source-based logic, so likelihood should be validated against that config's default behavior.

### Recommendation
- Require that `RecvError` (and any other packet type that mutates hostmap/tunnel state) only be honored if it is bound to session state that could only be known to a peer who has completed the authenticated handshake (e.g., authenticate `RecvError` similarly to the CloseTunnel path — inside the encrypted channel, or with an AEAD-protected tag keyed off `ConnectionState`) rather than relying on plaintext index + spoofable source-address comparison.
- Consider rate-limiting/requiring corroborating evidence (e.g., recent traffic activity, nonce challenge) before tearing down an established tunnel in response to an unauthenticated control message.

### Proof of Concept
A concrete PoC was not available in the index for `interface.go`'s `ShouldRecvError`/config defaults, so exact exploit preconditions (e.g., whether `listen.send_recv_error`/`accept_recv_error` defaults restrict acceptance) could not be fully verified in this pass. Conceptually: an attacker who observes (or brute-forces) a live tunnel's `RemoteIndex` and can spoof/appear to originate from that tunnel's current remote `ip:port` sends a bare `header.RecvError` packet; per `handleRecvError`, this triggers `f.closeTunnel(hostinfo)` and `f.handshakeManager.DeleteHostInfo(hostinfo)` on the victim, without the attacker ever presenting a certificate or completing a handshake. [4](#0-3)

### Citations

**File:** outside.go (L96-103)
```go
	// At this point we should have a valid existing tunnel, verify and send
	// recvError if necessary
	if hostinfo == nil || hostinfo.ConnectionState == nil {
		if !via.IsRelayed {
			f.maybeSendRecvError(via.UdpAddr, h.RemoteIndex)
		}
		return
	}
```

**File:** outside.go (L164-167)
```go
	case header.CloseTunnel:
		hostinfo.logger(f.l).Info("Close tunnel received, tearing down.", "from", via)
		f.closeTunnel(hostinfo)

```

**File:** outside.go (L522-539)
```go
func (f *Interface) maybeSendRecvError(endpoint netip.AddrPort, index uint32) {
	if f.sendRecvErrorConfig.ShouldRecvError(endpoint) {
		f.sendRecvError(endpoint, index)
	}
}

func (f *Interface) sendRecvError(endpoint netip.AddrPort, index uint32) {
	f.messageMetrics.Tx(header.RecvError, 0, 1)

	b := header.Encode(make([]byte, header.Len), header.Version, header.RecvError, 0, index, 0)
	_ = f.outside.WriteTo(b, endpoint)
	if f.l.Enabled(context.Background(), slog.LevelDebug) {
		f.l.Debug("Recv error sent",
			"index", index,
			"udpAddr", endpoint,
		)
	}
}
```

**File:** outside.go (L541-575)
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
