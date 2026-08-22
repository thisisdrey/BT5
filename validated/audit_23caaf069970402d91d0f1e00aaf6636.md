### Title
Unauthenticated `RecvError` packets allow anyone who knows a tunnel's `RemoteIndex` to remotely tear down a victim's tunnel - (File: outside.go)

### Summary
`Interface.readOutsidePackets` dispatches `header.RecvError` packets to `handleRecvError` before any handshake, certificate, or AEAD verification is performed. `handleRecvError` only checks that the claimed source `netip.AddrPort` matches the tunnel's currently recorded remote address — a value trivially forgeable over UDP — and then unconditionally tears down the tunnel via `f.closeTunnel(hostinfo)` and `f.handshakeManager.DeleteHostInfo(hostinfo)`. There is no proof that the sender possesses the tunnel's cryptographic keys or a CA-signed certificate, mirroring the missing access-control pattern from the reference report (any caller can force a state-changing action — there, cancelling another user's order; here, cancelling/terminating another peer's tunnel).

### Finding Description
In `outside.go`, `readOutsidePackets` handles `header.RecvError` completely outside the encrypted/authenticated packet path: [1](#0-0) 

That routes straight into `handleRecvError`, which performs only a source-address comparison — not a cryptographic check — before tearing down the hostinfo: [2](#0-1) 

The only gate is `hr.IsValid() && hr != addr`, i.e., a plain equality test on the UDP `AddrPort` reported in the packet's metadata. UDP source addresses are attacker-controlled/spoofable at the network layer and are not authenticated by any Nebula-level cryptography for this packet type (unlike `Message`/`Control`/`CloseTunnel`/`Control` packets, which are gated by `hostinfo.ConnectionState.Decrypt`/`VerifyRelay` earlier in the same function at lines 105–132, and unlike `CloseTunnel`, which is only processed after `Decrypt` succeeds at line 126 and 164-166). `RecvError` bypasses that entirely.

The only remaining requirement is knowledge of `h.RemoteIndex`, a 32-bit value used to look up the hostinfo via `f.hostMap.QueryReverseIndex(h.RemoteIndex)`. This index is observable on the wire (it is sent in cleartext in every packet header to/from that tunnel) or brute-forceable, and is not a secret credential.

### Impact Explanation
An attacker with no valid Nebula certificate — pure network-layer visibility/spoofing capability — can force the termination of an arbitrary peer's tunnel by sending a single unauthenticated UDP packet of type `header.RecvError` with:
- `RemoteIndex` set to the victim's local index for that tunnel (learned by observing traffic, or guessed), and
- a spoofed source `AddrPort` matching the tunnel's `hostinfo.GetRemote()`.

This causes `f.closeTunnel(hostinfo)` and `f.handshakeManager.DeleteHostInfo(hostinfo)` to run, dropping the tunnel and forcing re-handshake — a remote, unauthenticated denial-of-service against any active tunnel, directly analogous to the reference bug where any caller could `cancelMarketOrder` for `tradingAccountId` they didn't own, because the state-mutating function never verified caller identity/ownership before acting.

### Likelihood Explanation
Likelihood is bounded by the practicality of UDP source-address spoofing and of learning/guessing `RemoteIndex`. `RemoteIndex` is not secret (it appears unencrypted in the header of every packet on that tunnel, so any on-path or passive observer sees it), and UDP spoofing is a well-known, low-cost primitive when the attacker is on-path or the network permits egress spoofing. This is gated by `f.acceptRecvErrorConfig.ShouldRecvError(addr)` (a config-driven allow policy on that source address), so real-world exploitability depends on that configuration, but the core defect — no cryptographic authentication of the `RecvError` action — is present regardless.

### Recommendation
`handleRecvError` should not act on packet metadata alone. At minimum:
- Require `RecvError` handling to be authenticated (e.g., HMAC/keyed with the tunnel's established session key, or otherwise cryptographically bound to the hostinfo being torn down) rather than relying on address-equality of spoofable UDP fields.
- Alternatively, treat unauthenticated `RecvError` as advisory only (e.g., trigger a re-probe/handshake attempt) instead of unconditionally deleting hostinfo state.

### Proof of Concept
1. Establish/observe an active Nebula tunnel between `me` and `them`; note `RemoteIndex` from any packet header exchanged (or by triggering activity and sniffing).
2. Craft a `header.H{Type: header.RecvError, RemoteIndex: <observed index>}`-only packet (no payload) as done in `e2e/tunnels_test.go`'s `TestCloseTunnelAuthenticated` bogus-packet construction pattern: [3](#0-2) 
3. Send it via UDP with a spoofed source address equal to the victim's currently recorded remote (`hostInfo.CurrentRemote`).
4. Observe that `handleRecvError` calls `f.closeTunnel` and `hm.DeleteHostInfo`, terminating the tunnel without any handshake, certificate, or key material from the attacker.

Note: I could not find any explicit test in the indexed codebase covering unauthenticated `RecvError` spoofing (only `CloseTunnel` spoofing is explicitly tested and shown to be rejected in `TestCloseTunnelAuthenticated`), so this path appears to lack the same protection that `CloseTunnel` already has (`CloseTunnel` is only processed after successful AEAD decryption). This asymmetry is the crux of the finding.

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
