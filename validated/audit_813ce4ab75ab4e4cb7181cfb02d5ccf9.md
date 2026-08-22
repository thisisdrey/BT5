### Title
Unauthenticated `RecvError` packet can force-close an active, authenticated tunnel based solely on a spoofable UDP source address - (File: outside.go)

### Summary
The external report's bug class is: a handler that is reachable by an untrusted caller treats a *claim* that some privileged action occurred (an NFT transfer) as proof that it occurred, without verifying it. The closest reachable analog in Nebula is `handleRecvError` in `outside.go`, which tears down an already-established, cryptographically-authenticated tunnel purely because an unauthenticated `RecvError` packet arrived from a UDP address that matches the tunnel's current remote — with no cryptographic proof that the sender actually possesses the tunnel's session keys.

### Finding Description
`header.RecvError` packets are dispatched in `readOutsidePackets` before any certificate or session-key verification takes place, in the same unencrypted branch as handshake packets: [1](#0-0) 

`handleRecvError` then looks up the `HostInfo` by the claimed `RemoteIndex` and only checks that the sender's UDP source address happens to match the tunnel's currently known remote address before tearing the tunnel down: [2](#0-1) 

The only "verification" performed is `hr.IsValid() && hr != addr` — a plaintext, unauthenticated UDP source-address comparison. There is no AEAD tag, no Noise session-bound MAC, and no proof that the sender ever completed (or even attempted) the handshake for that tunnel. Just as `onERC721Received()` let a contract claim "the NFT was transferred" without the transfer actually happening, `handleRecvError` lets any packet sender claim "I couldn't decrypt your packet for this index" without ever having received (or having been able to decrypt) any packet at all. Both are pre-condition-bypasses: the callback/handler acts on an unverified claim of a prior event instead of an authenticated proof of it.

### Impact Explanation
An off-path or on-path attacker who can spoof (or naturally send from) the UDP source address currently associated with a victim's remote can force that victim to immediately tear down its authenticated Nebula tunnel to that peer (`f.closeTunnel(hostinfo)` and delete from the pending hostmap) with a single tiny unauthenticated packet — no valid certificate, no session keys, and no participation in the handshake are required. This is a remote state-poisoning / denial-of-service primitive against an already-secured tunnel, repeatable to keep forcing re-handshakes and disrupting connectivity.

### Likelihood Explanation
Reachability requires no CA-signed certificate and no prior handshake participation: the `RecvError` type is processed in the unencrypted dispatch path before any cert/session checks (`outside.go:81-84`). The only barrier is guessing/spoofing the correct `RemoteIndex` (a 32-bit value visible on the wire in every packet exchanged with that peer) and the current UDP source `ip:port` of one of the two tunnel endpoints, both of which are observable to any network-path observer and spoofable by many off-path attackers over UDP. The project's own `listen.accept_recv_error` config knob (`recvErrorAlways` default) and prior CHANGELOG entries about `recv_error`-related races/security tightening (#670, #1459) show this attack surface is a known, previously-adjusted risk area, reinforcing that it is a genuine, currently reachable weakness rather than a purely theoretical one.

### Recommendation
Do not act on `RecvError` (or any other pre-authentication claim) using only a UDP source-address match. Require that the teardown be corroborated by something the attacker cannot produce without the actual session, e.g.: rate-limit/ignore `RecvError` unless it is cryptographically bound (signed/MACed with the tunnel's established keys), or only trust it as a soft hint that triggers a fresh authenticated handshake/keepalive probe rather than an immediate `closeTunnel`, mirroring the report's fix pattern of "check the real event actually happened before honoring the side effect."

### Proof of Concept
1. Passively observe (or on-path capture) traffic between victim `A` and peer `B`; note `A`'s current `RemoteIndex` for the tunnel to `B` and `B`'s current UDP `ip:port` as seen by `A` (`hostinfo.GetRemote()`).
2. Craft a bare `header.RecvError` packet: `header.Encode(buf, header.Version, header.RecvError, 0, <A's index for B>, 0)`.
3. Send this packet to `A` with a spoofed (or otherwise obtained) source `ip:port` equal to `B`'s known remote address.
4. `A.handleRecvError` finds the matching `HostInfo`, sees `hr == addr`, and calls `f.closeTunnel(hostinfo)` plus `handshakeManager.DeleteHostInfo(hostinfo)` — the authenticated tunnel is destroyed without the attacker ever having valid Nebula certificates or session keys, reproducing the "state changed based on an unverified claim" bug class from the report.

### Citations

**File:** outside.go (L75-84)
```go
	// Unencrypted packets
	switch h.Type {
	case header.Handshake:
		f.handshakeManager.HandleIncoming(via, packet, h)
		return

	case header.RecvError:
		f.handleRecvError(via.UdpAddr, h)
		return
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
