### Title
Unauthenticated `RecvError` spoofing enables remote tunnel-teardown / state poisoning bypassing the intended source-check gate - (File: outside.go)

### Summary
The C4 report describes a missing state-gate: Lender's `mint` fails to check whether a market is `paused`, so an action that should be blocked by an existing protection mechanism is reachable anyway. The analogous pattern in nebula is `handleRecvError`, reachable by an attacker holding no CA-signed certificate at all. It has a gate (`hr != addr` source check) that is supposed to prevent unauthenticated third parties from tearing down someone else's tunnel, but the gate is a plaintext UDP source-address comparison that is trivially defeated by IP spoofing on a connectionless UDP transport, so the "pause"-equivalent protection is ineffective in practice.

### Finding Description
`RecvError` packets are dispatched before any certificate/HostInfo/decryption checks — they are handled in the unencrypted branch of `readOutsidePackets`: [1](#0-0) 

The handler looks up the hostinfo purely from the attacker-supplied 32-bit `RemoteIndex` in the header, and its only defense against a forged sender is comparing the UDP source address to the address nebula currently believes the peer is at: [2](#0-1) 

This is the same bug class as the C4 finding: a security-relevant gate exists ("is this legitimate/should this action be allowed") but is not actually enforced at the point where the sensitive action (`closeTunnel` / hostmap deletion) happens, because:
- `RemoteIndex` is only a 32-bit value and is observable by anyone who can capture or infer traffic on the path (it's sent in cleartext in every packet header, including handshake and data packets).
- The only "authentication" is `hr != addr`, i.e., comparing the UDP source `IP:port` on the packet to the cached remote address — this is exactly the kind of check that is meaningless against a spoofed UDP source address (UDP has no equivalent of a TCP handshake requiring reachability), so an off-path attacker who can spoof the source IP (or an on-path attacker) can present the correct expected `addr` and pass the gate without ever possessing a certificate.
- No cryptographic authentication (Noise/AEAD keys) is required to reach `f.closeTunnel(hostinfo)` and `f.handshakeManager.DeleteHostInfo(hostinfo)`.

This mirrors the `mint` bug precisely: a state-changing, security-relevant operation (teardown of an active, authenticated tunnel) is reachable through a code path that omits/weakens the check that is supposed to gate it, allowing a certificate-less attacker to disrupt legitimate protected sessions.

### Impact Explanation
An attacker with no CA-signed certificate can force teardown of any active tunnel between two legitimate, mutually-authenticated peers, provided they can guess or observe a `RemoteIndex` and spoof (or intercept) the corresponding UDP source address. This is a remote state-poisoning / denial-of-service primitive against the tunnel's `HostMap` state (`f.closeTunnel`, `hm.DeleteHostInfo`), directly analogous to bypassing a protective "pause" gate to perform an action that should have been blocked. Repeated abuse degrades availability of the mesh network and can be used to disrupt targeted host pairs at will.

### Likelihood Explanation
Reachability requires no valid certificate and no completed handshake — the packet is processed in the pre-authentication branch of `readOutsidePackets`. The only friction is guessing/observing a 32-bit index and spoofing a UDP source address, both of which are realistic for an attacker with network path visibility or basic spoofing capability, especially since `RemoteIndex` values are visible in cleartext in ordinary handshake/data headers traversing the same network.

### Recommendation
- Require the `RecvError` responder to prove possession of the underlying ConnectionState/cipher keys (e.g., authenticate the recv-error notification with the established AEAD keys or a signed token derived from the handshake) before acting on it, rather than relying solely on comparing the UDP source address.
- At minimum, rate-limit and corroborate `RecvError`-triggered teardown with additional liveness checks (e.g., require repeated failures across independently verified channels) before deleting hostmap state, rather than acting on a single unauthenticated packet.
- Consider restricting `RecvError` accept behavior more conservatively by default (`listen.recv_error`/`accept_recv_error` policy) and documenting that it is not a substitute for cryptographic authentication.

### Proof of Concept
1. Observe/capture any packet header exchanged between victim peers A and B (e.g., a data or handshake packet), extracting B's `RemoteIndex` as seen by A (present in cleartext in the header of every packet A receives from B).
2. Craft a UDP packet with `header.RecvError` type and that `RemoteIndex`, and spoof the source `IP:port` to match B's known current UDP endpoint (`hi.CurrentRemote`), as shown in the existing test harness pattern for injecting bogus control packets: [3](#0-2) 
3. Send this forged packet to A. `handleRecvError` finds the hostinfo via `QueryReverseIndex`, the spoofed source matches `hr`, and A tears down the tunnel to B (`closeTunnel` + `DeleteHostInfo`) despite the attacker never presenting a certificate or completing any handshake.

Note: I could not fully verify the default value of the `listen.recv_error`/`accept_recv_error` config (`acceptRecvErrorConfig`) within the available search results, so the exact out-of-the-box exposure (vs. opt-in) could not be confirmed with full certainty from the indexed content; a full-codebase review (e.g., a Devin session) would be needed to confirm default settings and any additional rate-limiting present elsewhere.

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

**File:** e2e/tunnels_test.go (L528-547)
```go
	buf := make([]byte, 1024)
	hdr := header.H{
		Version:        1,
		Type:           header.CloseTunnel,
		Subtype:        0,
		Reserved:       0,
		RemoteIndex:    hi.RemoteIndex,
		MessageCounter: 5,
	}
	out, err := hdr.Encode(buf)
	if err != nil {
		t.Fatal(err)
	}

	pkt := &udp.Packet{
		To:   hi.CurrentRemote,
		From: myHi.CurrentRemote,
		Data: out,
	}
	r.InjectUDPPacket(myControl, theirControl, pkt)
```
