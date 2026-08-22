### Title
Unauthenticated `RecvError` packet allows a spoofing attacker to force-close any established tunnel - (File: `outside.go`)

### Summary
`RecvError` is one of only two packet types dispatched before any decryption or PKI/cert verification (`outside.go:76-84`). Its handler, `handleRecvError`, tears down an established tunnel based solely on a plaintext `RemoteIndex` lookup and a UDP source-address equality check, neither of which is cryptographically authenticated. This mirrors the raffle-cancel bug class: an externally reachable state-destroying action gated only by a weak, non-cryptographic condition instead of proof of identity/possession of a valid session.

### Finding Description
In `readOutsidePackets`, header parsing happens first, and `RecvError` is handled in the same "Unencrypted packets" branch as `Handshake`, i.e. before the packet is matched to a `ConnectionState` and decrypted: [1](#0-0) 

`handleRecvError` then:
1. Checks a local rate/accept policy (`ShouldRecvError`) — not an authentication check, just a config gate.
2. Looks up `hostinfo` purely from the attacker-supplied `h.RemoteIndex` field taken from the plaintext header.
3. Only additional check is that the UDP source address matches the hostinfo's currently known remote address — a value that is never cryptographically bound to the packet and is trivially spoofable at the UDP layer. [2](#0-1) 

If both checks pass, it unconditionally tears down the tunnel: [3](#0-2) 

Critically, `RemoteIndex` is not a secret: it is carried in the clear in the header of *every* packet exchanged on the tunnel (message packets, close-tunnel packets, etc.), since header parsing in `readOutsidePackets` occurs before decryption. Any attacker who can observe a single packet on the path (or who previously held a session with the target) trivially learns a valid index value, and UDP has no source-address authentication, so spoofing the expected remote `addr` is straightforward. No CA-signed certificate, valid Noise handshake, or possession of any key material is required to trigger this teardown — contrasting with `CloseTunnel`, which is only reachable after successful AEAD decryption (`outside.go:164-166`) and is therefore authenticated.

This is analogous to the Winnables `cancelRaffle` bug: a state-destroying operation (`closeTunnel` + `DeleteHostInfo`) reachable by an unauthenticated third party, gated by a condition (source IP/port equality) that provides no real proof of authorization, analogous to the raffle contract's insufficiently-guarded `PRIZE_LOCKED` cancel check.

### Impact Explanation
An attacker with no valid certificate and no cryptographic material can force-close active tunnels between arbitrary legitimate peers by spoofing a single UDP packet, provided they know (or can observe) the target's current `RemoteIndex` and remote address — both of which travel unencrypted on the wire. Repeated abuse causes persistent denial of service on the mesh: victims must re-handshake continuously, and if timed against handshake completion, the attacker can perpetually prevent tunnel establishment, degrading the "protocol works as expected" invariant that nebula tunnels stay up. This is a remote, unauthenticated state-poisoning/DoS primitive against the core session lifecycle.

### Likelihood Explanation
Likelihood is high for any attacker positioned to observe traffic (on-path, a compromised router, or a previous peer of the target) since `RemoteIndex` and remote endpoint addresses are sent unencrypted in every packet header. UDP source spoofing is unauthenticated by design in this code path, so no additional cryptographic breakthrough is required — only observation of one packet plus the ability to spoof a UDP source, which is commonly achievable on many network paths.

### Recommendation
Do not let unauthenticated `RecvError` packets destroy live tunnel state based only on index + IP/port equality. Options:
- Require `RecvError` handling to include a MAC/signature bound to the current session key (e.g., verify using the hostinfo's `ConnectionState` similar to how `Message`/`CloseTunnel` packets are authenticated) before acting on it.
- At minimum, rate-limit and require several consecutive corroborating signals (e.g., combine with a short authenticated challenge) before tearing down an established tunnel, rather than acting on the very first plaintext packet.
- Treat `RecvError`-triggered closures as advisory only for hosts still in the handshake/pending state, and require cryptographic proof to close a fully-established tunnel.

### Proof of Concept
1. Attacker observes any single packet in either direction of an active nebula tunnel (or was previously peered with the target and recorded its own now-stale index/remote), learning `RemoteIndex` and the current UDP remote `addr` of the target hostinfo — both sent in cleartext headers per `outside.go:25-41` (`h.Parse(packet)` occurs before any decryption).
2. Attacker crafts a `RecvError` header packet (`header.Encode(..., header.RecvError, 0, index, 0)`, mirroring `sendRecvError` at `outside.go:528-539`) with `RemoteIndex` set to the learned index.
3. Attacker spoofs the UDP source address to match the target's expected remote `addr` for that hostinfo and sends the crafted packet to the victim's listener.
4. `handleRecvError` (`outside.go:541-575`) passes the `ShouldRecvError` gate, finds `hostinfo` via `QueryReverseIndex(h.RemoteIndex)`, sees `hr == addr`, and calls `f.closeTunnel(hostinfo)` plus `f.handshakeManager.DeleteHostInfo(hostinfo)` — destroying the tunnel state with zero cryptographic proof of authorization, and can be repeated indefinitely to prevent re-establishment.

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
