### Title
Unauthenticated `RecvError` packet allows remote tunnel teardown via spoofed source address - (File: outside.go)

### Summary
Nebula's `RecvError` message type is processed before any decryption or certificate/authentication step, and the only defense against a forged instance is a plaintext UDP source-address comparison. An attacker who can spoof the UDP source address (and knows or observes the 32-bit `RemoteIndex`) can trigger `closeTunnel`, tearing down an established, mutually-authenticated tunnel at will — the network-layer analog of `Destructible.destroy()`: a destructive action reachable "at any moment" without holding any credential (no CA-signed cert, no valid handshake), unlike the properly-authenticated `CloseTunnel` message path which requires a successfully decrypted AEAD packet.

### Finding Description
`readOutsidePackets` dispatches `header.RecvError` straight to `f.handleRecvError` before the packet undergoes any AEAD decryption or certificate check: [1](#0-0) 

`handleRecvError` looks up the hostinfo purely from the plaintext `RemoteIndex` field in the unauthenticated header, and only guards against spoofing by comparing the packet's *plaintext* source `netip.AddrPort` to the hostinfo's currently known remote address: [2](#0-1) 

If the attacker knows (or can spoof) the peer's remote UDP endpoint and the target's `RemoteIndex`, this check passes trivially — UDP source addresses are not cryptographically verified anywhere in this path. Once passed, the handler unconditionally calls `f.closeTunnel(hostinfo)` and `f.handshakeManager.DeleteHostInfo(hostinfo)`, destroying all tunnel/host state: [3](#0-2) 

This stands in sharp contrast to the legitimate `header.CloseTunnel` message type, which is only processed *after* successful AEAD decryption — i.e., it requires possession of the established session keys derived from a verified handshake: [4](#0-3) 
The `e2e` test suite explicitly documents and enforces that a bogus, unauthenticated `CloseTunnel` packet is rejected because it fails decryption: [5](#0-4) 

No equivalent authenticity requirement exists for `RecvError`. This mirrors the reported bug class: two code paths achieve the same destructive effect (tearing down the tunnel), but one path (`RecvError`) bypasses the protections (authentication) that guard the other (`CloseTunnel`), and can be invoked "at any moment" by anyone who can spoof a packet, not just a party holding a valid, CA-signed identity.

### Impact Explanation
An off-path or on-path attacker capable of spoofing UDP source address/port (a well-known, low-cost network-layer capability, especially for on-path/adjacent attackers or via reflection) can force termination of any live Nebula tunnel without any cryptographic credential. This is a remote, unauthenticated denial-of-service / remote state poisoning primitive: it deletes the hostinfo from both the main hostmap and the pending handshake map, forcing renegotiation and disrupting connectivity repeatedly. Because certificate identity plays no role in this path, an attacker with no CA-signed certificate at all can exploit it, matching the "reachable by an attacker with no CA-signed certificate" scope of this scan.

### Likelihood Explanation
The codebase itself already acknowledges the spoofing risk ("Someone spoofing recv_errors?" log message) and gates acceptance behind `acceptRecvErrorConfig.ShouldRecvError(addr)`, a configuration knob (`listen.send_recv_error`) documented in the changelog as a known security trade-off ("Sending these messages can expose the fact that Nebula is running on a host"). This indicates the RecvError mechanism was already flagged as a security-sensitive feature, but the mitigation is purely a source-address string compare on unauthenticated UDP, not a cryptographic proof — the exact same "should be restricted but is not really restricted" pattern as `Destructible.destroy()` being callable regardless of `Pausable` state. Exploitability depends on the attacker's ability to spoof source address/port and to know or guess the target's `RemoteIndex`, so likelihood is moderate rather than trivial for a purely off-path attacker, but is high for anyone on-path or able to observe traffic between the two peers.

### Recommendation
Do not allow any state-destroying action to be taken on the basis of an unauthenticated, unencrypted control message. Either:
1. Require `RecvError` handling to additionally verify a token/HMAC bound to the current session key (something only a party that has completed the real handshake could produce), or
2. Downgrade `RecvError`'s effect so that it only marks the tunnel for extra scrutiny/faster rehandshake retry rather than immediately calling `closeTunnel`/`DeleteHostInfo`, or
3. Require multiple corroborating signals (e.g., decrypted `CloseTunnel`, inactivity timeout, or several successive `RecvError`s from the exact matching source over time) before tearing down state, similar to how `connectionManager`'s traffic-check state machine already handles other teardown decisions.

### Proof of Concept
1. Attacker observes or infers the victim's active Nebula tunnel `RemoteIndex` (visible in cleartext in every packet header exchanged between two established peers, e.g. via a compromised on-path relay/lighthouse vantage point or packet capture).
2. Attacker crafts a UDP packet with `header.RecvError` type and the observed `RemoteIndex`, and spoofs the source address to match the victim's currently known remote endpoint for that hostinfo.
3. Attacker sends this single unauthenticated UDP packet to the victim's listen port.
4. `readOutsidePackets` → `handleRecvError` passes the address-match check (source spoofed to equal `hostinfo.GetRemote()`), then unconditionally invokes `f.closeTunnel(hostinfo)` and `f.handshakeManager.DeleteHostInfo(hostinfo)`, destroying the live tunnel state with no cryptographic proof of authorization.

Verification limits: I was unable to fully trace `f.acceptRecvErrorConfig.ShouldRecvError` in `interface.go` (only grep hits, not the full implementation/default config) within the remaining exploration budget, so I cannot confirm whether it is enabled by default or opt-in in this specific build; this should be checked directly in `interface.go` before treating likelihood as "default-enabled."

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

**File:** outside.go (L126-167)
```go
	out, err = hostinfo.ConnectionState.Decrypt(f.l, h.MessageCounter, out, packet, nb)
	if err != nil {
		if f.l.Enabled(context.Background(), slog.LevelDebug) {
			hostinfo.logger(f.l).Debug("Failed to decrypt packet", "error", err, "from", via, "header", h)
		}
		return
	}

	// Roam before we respond
	f.handleHostRoaming(hostinfo, via)
	f.connectionManager.In(hostinfo)

	switch h.Type {
	case header.Message:
		switch h.Subtype {
		case header.MessageNone:
			f.handleOutsideMessagePacket(hostinfo, out, packet, fwPacket, nb, q, localCache)
		default:
			hostinfo.logger(f.l).Error("IsValidSubType was true, but unexpected message subtype seen", "from", via, "header", h)
			return
		}

	case header.LightHouse:
		//TODO: assert via is not relayed
		lhf.HandleRequest(via.UdpAddr, hostinfo.vpnAddrs, out, f)

	case header.Test:
		switch h.Subtype {
		case header.TestReply:
			// No-op, useful for the Roaming and connectionManager side-effects above
		case header.TestRequest:
			//recycle the input packet ciphertext as our output buffer
			f.send(header.Test, header.TestReply, hostinfo.ConnectionState, hostinfo, out, nb, packet)
		default:
			hostinfo.logger(f.l).Error("IsValidSubType was true, but unexpected test subtype seen", "from", via, "header", h)
			return
		}

	case header.CloseTunnel:
		hostinfo.logger(f.l).Info("Close tunnel received, tearing down.", "from", via)
		f.closeTunnel(hostinfo)

```

**File:** outside.go (L250-257)
```go
// closeTunnel closes a tunnel locally, it does not send a closeTunnel packet to the remote
func (f *Interface) closeTunnel(hostInfo *HostInfo) {
	final := f.hostMap.DeleteHostInfo(hostInfo)
	if final {
		// We no longer have any tunnels with this vpn addr, clear learned lighthouse state to lower memory usage
		f.lightHouse.DeleteVpnAddrs(hostInfo.vpnAddrs)
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
