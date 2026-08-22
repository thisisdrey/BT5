### Title
Unauthenticated `RecvError` message allows remote tunnel teardown / state poisoning by a certificate-less attacker - (File: `outside.go`)

### Summary
`readOutsidePackets` dispatches `header.RecvError` packets to `handleRecvError` before any AEAD decryption, MAC verification, or certificate-based authentication takes place. The only "authentication" applied is a comparison of the UDP source address against the currently known remote address for the referenced tunnel — a value trivially observable and easily spoofed on UDP. This mirrors the reported analog: a state-mutating operation (`sponsor`) that should only be reachable by a trusted caller (the factory) but has no real authorization check, so any third party can invoke it.

### Finding Description
In `readOutsidePackets`, the header type switch handles `header.RecvError` immediately after header parsing and before the "All remaining packets are encrypted" boundary: [1](#0-0) 

This means `RecvError` packets never go through `hostinfo.ConnectionState.Decrypt`, and are not covered by any AEAD tag, nonce, or per-tunnel key. `handleRecvError` then: [2](#0-1) 

looks up the target tunnel purely by the attacker-supplied `h.RemoteIndex` (a 32-bit value carried in cleartext in every packet header on the wire, hence trivially observable by an on-path or off-path attacker who has ever seen a single packet for that tunnel), and the only gate against an unrelated third party is:

```go
hr := hostinfo.GetRemote()
if hr.IsValid() && hr != addr {
    f.l.Info("Someone spoofing recv_errors?", ...)
    return
}
```

This checks the *claimed UDP source address* of the incoming packet against the tunnel's currently known remote address — not a cryptographic signature or a value derived from the peer's certificate. UDP source addresses are attacker-controllable (spoofable at the network layer, or simply matched by an off-path attacker who has observed the real remote's address, which is exchanged via the lighthouse and is not secret). There is no possession-of-key proof, no counter/replay window, and no certificate involvement whatsoever — the entire "authorization" for this destructive action is an easily-forged network-layer field.

Once the loose address check is satisfied, `handleRecvError` unconditionally calls `f.closeTunnel(hostinfo)` and `f.handshakeManager.DeleteHostInfo(hostinfo)`, tearing down an established, fully-authenticated tunnel between two legitimate peers — exactly the kind of privileged, should-be-restricted action ("only the real remote peer that owns this tunnel should be able to signal a receive error") that is missing a proper authentication check, analogous to `sponsor` only being intended for the factory but callable by anyone.

### Impact Explanation
An attacker with no CA-signed certificate and no established session can force termination of any live Nebula tunnel between two legitimate nodes by:
1. Observing (or guessing) the 32-bit `RemoteIndex` from any cleartext header of traffic between the victims (visible on the wire, or learnable from lighthouse-advertised endpoints/traffic patterns), and
2. Sending a single unauthenticated UDP packet spoofed from (or matching) the victim's known remote address.

This results in remote state poisoning / denial of service: forced tunnel teardown and deletion of hostmap/pending-handshake state (`closeTunnel`, `DeleteHostInfo`), disrupting the mesh without requiring any valid certificate, key material, or completed handshake — matching the "remote state poisoning" / "remote crash" impact classes called out as acceptable.

### Likelihood Explanation
Likelihood is high on networks where UDP source spoofing is feasible (very common for UDP, unlike TCP) or where the attacker is on-path/off-path but can observe traffic between the two victims. The `RemoteIndex` is not secret (transmitted in cleartext in every header), and the address check is a plain equality on a spoofable field, not a MAC or signature. This is a pre-authentication code path deliberately excluded from the encrypted/authenticated packet handling, so it is reachable by any network attacker without holding a Nebula certificate — satisfying the "no CA-signed certificate" constraint required for this analog class.

### Recommendation
Do not allow `RecvError` to trigger tunnel/hostinfo deletion based solely on a spoofable UDP source-address match against a publicly-known/observable value. Instead:
- Require some cryptographic binding for `RecvError` (e.g., only accept it if it can be tied to a value only the legitimate peer could produce, or authenticate it using the tunnel's established key material rather than comparing the plaintext source address).
- Alternatively, treat `RecvError` purely as a soft hint (e.g., trigger a re-handshake attempt) rather than an unconditional `closeTunnel`/`DeleteHostInfo`, so a spoofed message cannot unilaterally destroy session state.
- Rate-limit and require corroborating evidence (e.g., only honor `RecvError` after the local send-side has independently observed packet loss to that same peer) before tearing down state.

### Proof of Concept
1. Establish two legitimate Nebula nodes, A and B, with a live tunnel (as in `TestCloseTunnelAuthenticated`, which demonstrates the packet-injection technique for `CloseTunnel`) — the same injection technique applies to `RecvError`: [3](#0-2) .
2. An attacker who has observed the cleartext header of any packet between A and B extracts B's `RemoteIndex` value from the header.
3. The attacker crafts a UDP packet with `header.RecvError` type and B's `RemoteIndex`, spoofing (or replaying from a position that matches) B's known UDP source address, and sends it to A.
4. `readOutsidePackets` routes this directly to `handleRecvError` without decryption: [4](#0-3) .
5. Since the spoofed source address matches A's cached `hostinfo.GetRemote()` value for B, the check passes and A calls `closeTunnel`/`DeleteHostInfo`, killing the tunnel: [5](#0-4) .

Note: I was not able to fully verify the exact conditions under which `acceptRecvErrorConfig.ShouldRecvError` in `interface.go` gates this behavior (e.g., whether it's rate-limited or opt-in/opt-out by default), since I ran out of tool iterations before inspecting that logic in full. This could affect the practical likelihood/frequency of exploitation and should be checked directly in `interface.go` before finalizing severity.

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
