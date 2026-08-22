### Title
Unauthenticated `RecvError` and `CloseTunnel` control packets let a griefer tear down a legitimate tunnel using only an observable index - ([File: outside.go])

### Summary
The reported bug pattern is a griefing/DoS: an attacker observes a value that is only meant to be consumed once by a legitimate multi-step flow (the signature/nonce in `delegateBySig`), and independently submits it first so the real transaction fails, blocking the legitimate user. Nebula's equivalent reachable surface is the plaintext, unauthenticated `RecvError` and `CloseTunnel` control message types processed in `readOutsidePackets`. Both are dispatched and acted upon **before** any AEAD/cert verification, keyed only on `h.RemoteIndex`, a value that is sent in cleartext on every data-plane packet and is therefore fully observable to anyone who can see traffic between two peers.

### Finding Description
In `outside.go`, `RecvError` is handled immediately, ahead of the encrypted-packet checks: [1](#0-0) 

`handleRecvError` only defends against spoofing by comparing the *source UDP address* of the packet to the hostinfo's currently known remote address — there is no cryptographic authentication of the `RemoteIndex` field itself: [2](#0-1) 

If the check passes (source address matches, e.g. because the attacker can spoof the UDP source, or is on-path, or is simply relaying an observed value from a position that lets the address check pass), the tunnel is torn down via `f.closeTunnel(hostinfo)` and the pending handshake is deleted, allowing a fresh handshake to be forced.

`CloseTunnel` is handled similarly but *after* AEAD decryption succeeds — however it still tears down the tunnel unconditionally on receipt with no rate limiting or confirmation handshake: [3](#0-2) 

The `RemoteIndex` used to route/authenticate these control messages is not secret: it is embedded in cleartext in every message header exchanged between two peers (see the header encoding used elsewhere, e.g. `header.Encode` for `RecvError`): [4](#0-3) 

This mirrors the report's bug class precisely: a value that legitimately flows as part of a multi-step, session-bound operation (there, a signature+nonce; here, the `RemoteIndex`) is observable by a third party who has no valid credentials (no CA-signed cert, no participation in the Noise handshake) and can be used out-of-band to disrupt/terminate the legitimate session before/during its normal lifecycle, exactly the "griefer blocks user" impact described in the report.

### Impact Explanation
An attacker who can observe traffic between two Nebula peers (or spoof the UDP source address of one of them) can extract the `RemoteIndex` from any observed packet and send a forged `RecvError`, causing `handleRecvError` to call `f.closeTunnel(hostinfo)` — tearing down an active, legitimate tunnel. This is a remote state-poisoning/DoS impact: it directly blocks legitimate communication, forcing repeated re-handshakes, which is functionally identical to "blocking a user from completing their intended operation" in the original report.

### Likelihood Explanation
Likelihood depends on the attacker being able to either (a) observe the `RemoteIndex` on the wire (trivial for any on-path or off-path observer capturing UDP traffic, since it is unencrypted) and (b) get a packet accepted by the address-match check in `handleRecvError`, which is possible via UDP source-address spoofing when the peer's public UDP endpoint is known (lighthouses publish these addresses by design). There is `maybeSendRecvError`/`ShouldRecvError` rate-limiting on some paths, but the core mechanism does not cryptographically bind `RecvError` to the session.

### Recommendation
Do not act on `RecvError` (or, for extra safety, `CloseTunnel`) based solely on an unauthenticated index + source-address match. Require these control messages to be authenticated (e.g., wrapped in the encrypted/AEAD tunnel like `CloseTunnel` already is, or accompanied by a MAC keyed off session material) before triggering `closeTunnel`, and/or add stricter rate limiting and confirmation (e.g., require a full round trip / retry-tolerant teardown rather than single-packet unilateral teardown).

### Proof of Concept
1. Attacker passively observes UDP traffic between Host A and Host B (or otherwise learns their public UDP endpoints from lighthouse data, which is normal Nebula operation) and records the `RemoteIndex` value carried in the packet headers exchanged between them — this requires no CA-signed certificate or participation in the handshake.
2. Attacker crafts a `RecvError` packet using `header.Encode(..., header.RecvError, 0, index, 0)` with the observed `index`, spoofing the UDP source address to match Host B's known public endpoint, and sends it to Host A: [4](#0-3) 
3. `readOutsidePackets` dispatches it directly to `handleRecvError` without any decryption/authentication: [5](#0-4) 
4. `handleRecvError` finds the hostinfo by `RemoteIndex`, sees the (spoofed) source address matches the current remote, and calls `f.closeTunnel(hostinfo)`, tearing down the legitimate tunnel and deleting the pending handshake state: [6](#0-5) 
5. Host A's legitimate tunnel to Host B is destroyed, forcing renegotiation and denying service — analogous to the griefer blocking the airdrop claim by consuming the shared nonce out-of-band.

### Citations

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

**File:** outside.go (L164-166)
```go
	case header.CloseTunnel:
		hostinfo.logger(f.l).Info("Close tunnel received, tearing down.", "from", via)
		f.closeTunnel(hostinfo)
```

**File:** outside.go (L528-539)
```go
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

**File:** outside.go (L541-574)
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
```
