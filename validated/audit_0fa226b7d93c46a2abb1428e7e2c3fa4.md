### Title
Unauthenticated `RecvError` packet allows any attacker to remotely tear down an established Nebula tunnel - (File: outside.go)

### Summary
The `M-3` report describes a Teller Finance function where a state-changing "revoke" action (`revokeLender`) can be triggered by any caller because the code never verifies that the caller is authorized to revoke that specific stakeholder. The reachable analog in this codebase is `handleRecvError` in `outside.go`, which tears down an established, authenticated tunnel in response to an unauthenticated, plaintext `RecvError` packet type, relying only on a spoofable source-address comparison instead of any cryptographic proof of the sender's identity.

### Finding Description
`RecvError` is dispatched before any tunnel/session state is checked, directly out of the unencrypted header switch in `readOutsidePackets`: [1](#0-0) 

`handleRecvError` looks up the hostinfo purely by the `RemoteIndex` field taken from the plaintext header, and the only "authentication" applied is comparing the packet's UDP source address against the hostinfo's currently known remote address: [2](#0-1) 

Unlike `CloseTunnel`, which is sent as an encrypted/authenticated message inside the tunnel's Noise session and is validated via AEAD decryption and message counters (see `TestCloseTunnelAuthenticated`, which shows a forged `CloseTunnel` packet without valid encryption is rejected): [3](#0-2) 

`RecvError` carries no cryptographic authentication at all — it is a bare header with a `RemoteIndex`, and the "spoofing" check is only an IP:port string comparison, not a certificate/signature/MAC check: [4](#0-3) 

This is directly analogous to the reported bug class: a privileged/destructive action (revoke a lender / tear down a tunnel) is reachable by an unauthorized party because access control relies on an easily-forged attribute (an arbitrary caller address in Solidity, a spoofable UDP source address here) rather than a cryptographic authentication check.

### Impact Explanation
If an attacker can guess or observe a victim's active tunnel's `RemoteIndex` (a 32-bit value exchanged during handshake and visible on the wire, or observable if the attacker is on-path or can sniff/predict UDP traffic) and can spoof UDP packets with the victim's real peer's source `netip.AddrPort` (trivial on many networks without egress/ingress filtering, or from an on-path/off-path attacker on the internet), they can force `f.closeTunnel(hostinfo)` and `f.handshakeManager.DeleteHostInfo(hostinfo)` to execute, destroying a legitimate, fully-authenticated tunnel between two honest Nebula nodes. This is a remote denial-of-service / connectivity disruption against arbitrary peers in the mesh, matching the "make the market unusable" impact in the source report, translated to "make the mesh network connection unusable."

### Likelihood Explanation
Exploitability depends on: (1) knowledge of the target's current `RemoteIndex`, which is not secret-strength protected (32-bit, sent in cleartext handshake/message headers, and could be brute forced or observed on path), and (2) the ability to spoof a UDP source address matching the current remote endpoint, which is a well-known weakness of UDP-based protocols absent BCP38/anti-spoofing filtering on the path. `ShouldRecvError`/`acceptRecvErrorConfig` gate whether `RecvError` is accepted at all (configurable, e.g., `listen.recv_error` settings noted in the CHANGELOG history) so exposure varies per deployment, but where enabled, no cryptographic check protects the teardown action.

### Recommendation
Do not act on `RecvError` based solely on a source-address match. Either remove/deprecate the unauthenticated `RecvError` fast path in favor of the already-authenticated `CloseTunnel` message inside the established Noise session, or require some additional proof (e.g., HMAC/keyed check derived from the session, or rate-limited/soft response that requires re-handshake confirmation rather than immediate teardown) before destroying hostinfo state in response to a plaintext, unauthenticated packet.

### Proof of Concept
Not independently reproduced in this session; a PoC would require crafting a plaintext `header.RecvError` packet with the target's known `RemoteIndex`, sent from a spoofed UDP source address matching `hostinfo.GetRemote()`, and observing that `handleRecvError` triggers `f.closeTunnel` and `f.handshakeManager.DeleteHostInfo` without any cryptographic validation, as shown at [2](#0-1) . Confirming exact default values of `acceptRecvErrorConfig`/`ShouldRecvError` (in `interface.go`) was not completed before the session ended, so the precise default exposure (always-on vs. opt-in) is unverified.

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

**File:** e2e/tunnels_test.go (L528-558)
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
	r.Log("Injected bogus close tunnel. Let's see!")
	waitStart = time.Now()
	for {
		myIndexes := myControl.GetHostmapIndexCount()
		theirIndexes := theirControl.GetHostmapIndexCount()
		if myIndexes == 0 {
			t.Fatal("myIndexes should not be 0")
		}
		if theirIndexes == 0 {
			t.Fatal("theirIndexes should not be 0, they should have rejected this bogus packet")
		}
```
