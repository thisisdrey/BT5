### Title
Unauthenticated `RecvError` packets allow spoofed tunnel teardown, forcing costly re-handshake - (File: `outside.go`)

### Summary
`RecvError` is one of only two unencrypted header types (the other being `Handshake`) that `readOutsidePackets` dispatches *before* any peer certificate/`ConnectionState` check or decryption occurs. `handleRecvError` accepts this plaintext, unauthenticated packet and, if its `RemoteIndex` matches a live tunnel and the sender's UDP address loosely matches the recorded remote, immediately tears the tunnel down. This mirrors the report's bug class: a state-changing action ordinarily expected to originate from a specific trusted party (there, `PublicLock.initialize()`; here, tunnel teardown) is reachable by anyone with no cryptographic proof of identity, at a cost asymmetric to the victim (one spoofed UDP datagram vs. a full Noise handshake to recover).

### Finding Description
`readOutsidePackets` parses the header and, before decrypting anything or validating any certificate, branches directly into handshake/`RecvError` handling: [1](#0-0) 

`handleRecvError` then looks up the hostinfo purely by the plaintext `RemoteIndex` field (`QueryReverseIndex`) and, if the source `AddrPort` happens to equal the currently recorded remote for that tunnel, deletes it: [2](#0-1) 

No MAC, signature, or any part of the authenticated Noise session is checked. `RemoteIndex` (the local index chosen by the intended peer) travels in the cleartext header of every ordinary data/handshake packet on the wire, so any passive observer of a tunnel's traffic — who need not hold any CA-signed certificate at all — can read it directly. UDP source addresses are trivially spoofable by an on-path or off-path attacker who can predict/observe the victim's `AddrPort`; the only defense here is `hr != addr`, an address comparison, not an authentication check comparable to the certificate/CA-pool verification that gates every other privileged path (`handshake.CertVerifier`, `validatePeerCert`).

This is structurally the same class of bug as the report: a function that mutates critical state (`PublicLock.initialize()` there; tunnel teardown here) has no access control tied to the legitimate, certificate-verified identity, so an attacker who never proved possession of a CA-signed cert can trigger it and force the legitimate party into expensive recovery work.

### Impact Explanation
An attacker who can observe a victim's plaintext header traffic (or spoof its known/observed UDP `AddrPort`) can send a single unauthenticated `RecvError` packet to force `closeTunnel` + `DeleteHostInfo` on the target, at any time, without ever completing (or even attempting) a handshake or presenting a certificate. The victim must then perform an entire new Noise `IX` handshake (asymmetric-key operations, certificate exchange, CA-pool verification) to restore connectivity — a costly, repeatable denial-of-service that can be re-triggered indefinitely at negligible cost to the attacker, directly analogous to the "malicious user forcing costly redeploy" impact described in the report.

### Likelihood Explanation
Reachability requires only: (1) knowledge of the target's `RemoteIndex`, obtainable by passively observing any packet on that tunnel since indices are unencrypted header fields, and (2) the ability to spoof or match the current remote `AddrPort`, which is feasible for on-path attackers and in many NAT/UDP environments. No CA-signed certificate, valid handshake, or decryption capability is needed, satisfying the "no CA-signed certificate" reachability constraint. The project's own history (`listen.send_recv_error`, `acceptRecvErrorConfig`/`ShouldRecvError` gating, and the CHANGELOG entry noting recv_error can "expose the fact that Nebula is running on a host") shows this exact packet type has previously been recognized as a spoofing/DoS-relevant surface.

### Recommendation
Do not allow an unauthenticated, unencrypted packet type to unilaterally tear down an authenticated tunnel. Require `RecvError` handling to be tied to session-authenticated proof (e.g., only accept it once wrapped/verified inside the encrypted channel, or require a MAC/counter bound to the session's Noise keys) rather than relying solely on an index match plus a spoofable source-address comparison in `handleRecvError`.

### Proof of Concept
1. Passively observe (or otherwise learn) any packet exchanged on an established Nebula tunnel between `me` and `them`; extract the cleartext `RemoteIndex` from the header (as demonstrated by `TestCloseTunnelAuthenticated` in `e2e/tunnels_test.go`, which crafts a bogus `header.CloseTunnel` packet using `hi.RemoteIndex` and `hi.CurrentRemote`/`myHi.CurrentRemote`): [3](#0-2) 
2. Craft a `header.RecvError` packet with that `RemoteIndex`, sourced from (or spoofed as) the victim's currently recorded remote `AddrPort`.
3. Inject it toward the other peer; `handleRecvError` finds the hostinfo via `QueryReverseIndex`, confirms `hr == addr`, and calls `f.closeTunnel(hostinfo)` plus `f.handshakeManager.DeleteHostInfo(hostinfo)` — tearing down the tunnel with no certificate or session-key verification: [4](#0-3) 
4. The victim must now perform a full handshake (certificate exchange + CA-pool verification) to re-establish connectivity, which the attacker can repeat at will.

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
