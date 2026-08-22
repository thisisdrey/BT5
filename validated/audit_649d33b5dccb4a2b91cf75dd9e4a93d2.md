### Title
Unauthenticated `RecvError` packet allows spoofed remote-index+source-address to force-teardown an established, authenticated tunnel - (File: outside.go)

### Summary
The `selfdestruct`-based bug in FixedPrice/OpenEdition destroys contract state as a side effect of an ordinary buyer action, and any transaction landing after that destruction is silently accepted while its value is permanently lost — a state-destroying operation that is reachable without the caller proving any special right to trigger it, causing loss for a party who did nothing wrong. The reachable analog in nebula is `header.RecvError` handling: it is processed *before* any AEAD/cert-derived authentication is applied to the packet, and if its two easily-satisfied conditions line up (a guessable/observed 32-bit local index, and a spoofed UDP source address matching the victim's `hostinfo.remote`), it tears down a fully-established, mutually-authenticated tunnel and its cryptographic state.

### Finding Description
In `readOutsidePackets`, `header.RecvError` is dispatched immediately after header parsing and before decryption/cert checks — unlike `header.Message`/`header.CloseTunnel`, it never passes through `hostinfo.ConnectionState.Decrypt`: [1](#0-0) 

`handleRecvError` is the handler: [2](#0-1) 

It looks the target hostinfo up by the *cleartext* `h.RemoteIndex` field via `QueryReverseIndex`, and the only check binding this unauthenticated packet to the real peer is that the arriving UDP source address (`addr`, taken from the underlay packet, i.e. attacker-controlled/spoofable) equals the hostinfo's currently known remote address (`hr`). No certificate, no AEAD tag, no handshake state, and no `MessageCounter`/replay window is checked at all — this packet type never carries an authenticated counter the way `header.Message` and `header.CloseTunnel` do (those are gated by `hostinfo.ConnectionState.Decrypt` first, in the same function, at line 126). If both conditions hold, `f.closeTunnel(hostinfo)` and `f.handshakeManager.DeleteHostInfo(hostinfo)` unconditionally destroy the live tunnel state — the equivalent of `selfdestruct` on a live, funded contract triggered by an unauthenticated actor rather than the contract owner/participant.

By contrast, the legitimate `header.CloseTunnel` type is gated behind full AEAD decryption using session keys established through the CA-verified Noise handshake: [3](#0-2) [4](#0-3) 
This is the "correct" pattern (authenticated destruction), and the project's own e2e test explicitly documents and defends this invariant for `CloseTunnel`: [5](#0-4) 
No equivalent authenticated-spoof test or counter/allow-list gate exists for `RecvError` beyond the rate-limiting `ShouldRecvError` config, which throttles frequency but does not authenticate the sender or the claimed index/address pairing.

The project's own changelog corroborates that `recv_error` has been a known soft spot requiring hardening: "Disable sending recv_error messages when a packet is received outside the allowable counter window" (v1.9.7), showing prior awareness of abuse potential around this unauthenticated control channel, though that fix constrained *sending* recv_error, not the trust placed in *receiving* one. [6](#0-5) 

### Impact Explanation
`RecvError` is accepted from any UDP source claiming to be the peer's current remote address, with the only "credential" being knowledge of the 32-bit `localIndexId` the target assigned to that tunnel — a value that appears in cleartext in the header of every ordinary data/message packet a legitimate peer exchanges with that host. An attacker positioned to observe traffic (or simply guess indexes over many established tunnels on a busy relay/lighthouse-adjacent host) and able to spoof the UDP source address of the legitimate remote peer can force `closeTunnel` + `DeleteHostInfo` on a fully authenticated, live tunnel between two certificate-holding peers. This:
- destroys negotiated session keys and hostmap/relay state for that tunnel (remote state poisoning),
- causes any packets legitimately in flight at that moment, or arriving from the real peer shortly after, to be dropped (since the encrypted session no longer exists) until a fresh handshake occurs — directly mirroring the "value silently locked in a destroyed contract" impact of the original finding, except here it is data/availability rather than ETH,
- can be repeated to persistently disrupt the mesh (DoS via repeated forced teardown), a remote-triggerable, unauthenticated denial of a specific established tunnel.

This is reachable purely by spoofing UDP packets — no CA-signed certificate, no valid handshake completion, and no possession of any key material is required, satisfying the "attacker with no CA-signed certificate" constraint.

### Likelihood Explanation
Likelihood is moderate: exploitation requires (1) UDP source-address spoofing capability toward the victim (feasible for a large class of off-path/network-level attackers, particularly since nebula explicitly warns elsewhere in the codebase about spoofable UDP source addresses, e.g. the roaming-suppression and remote-allow-list logic built specifically to blunt spoofing) and (2) knowledge of the victim's 32-bit local index for that specific tunnel, which is transmitted in cleartext on every packet of that tunnel and is therefore observable to any on-path attacker or anyone who can capture even one packet of the target tunnel. The existing rate limiter (`ShouldRecvError`) reduces repeated abuse but does not prevent a single well-timed spoofed packet.

### Recommendation
- Require the `RecvError` handler to validate more than "source-address equality with the last known remote" — bind acceptance to a value derived from the authenticated session (e.g., only tear down state in response to a `RecvError` if it can be correlated with an actually-sent, counter-tracked outbound message from this node, not merely a matching index/address pair).
- Consider requiring `RecvError` to be authenticated (e.g., MAC'd with a value derivable only by a peer that has actually established (or previously established) the session), rather than accepting it purely in cleartext.
- Tighten the remote-address check to use the same anti-spoof defenses already applied to roaming (`AllowAll`/remote allow-list gating), rather than a raw equality check against `hostinfo.GetRemote()`.
- Ensure `DeleteHostInfo`/`closeTunnel` triggered by `RecvError` cannot be used to bypass `drop_inactive`/reconnection backoff protections that limit forced-teardown churn.

### Proof of Concept
1. Establish a legitimate, fully authenticated tunnel between Alice and Bob (both hold CA-signed certs), completing the Noise handshake normally.
2. Attacker Eve, observing traffic (or having captured one prior packet of this tunnel), learns Bob's `localIndexId` for the tunnel with Alice from the cleartext header field carried in every packet Bob sends.
3. Eve crafts a UDP packet with header `Type = header.RecvError`, `RemoteIndex = <Bob's localIndexId for Alice's tunnel>`, and spoofs the source IP/port to match Alice's currently known remote address (`hi.CurrentRemote`), sending it to Bob — exactly as constructed for the legitimate `CloseTunnel` PoC already present in the test suite: [7](#0-6) 
   (swap `header.CloseTunnel` for `header.RecvError` and omit MessageCounter/encryption, since `RecvError` skips AEAD entirely per the dispatch in `readOutsidePackets`.)
4. On Bob, `handleRecvError` finds the hostinfo via `QueryReverseIndex`, sees the spoofed source address matches `hostinfo.GetRemote()`, and calls `f.closeTunnel(hostinfo)` + `f.handshakeManager.DeleteHostInfo(hostinfo)` — destroying the live, authenticated tunnel and any in-flight session state, without Eve ever holding a certificate or completing a handshake.

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

**File:** outside.go (L105-132)
```go
	if len(packet) < header.Len+hostinfo.ConnectionState.dKey.Overhead() {
		f.messageMetrics.RxInvalid(1)
		if f.l.Enabled(context.Background(), slog.LevelDebug) {
			f.l.Debug("packet too small", "from", via, "length", len(packet))
		}
		return
	}

	// All remaining packets are encrypted
	if isMessageRelay {
		// Relay packets are special, this branch should always early-return
		if err = hostinfo.ConnectionState.VerifyRelay(f.l, h.MessageCounter, packet, nb); err != nil {
			if f.l.Enabled(context.Background(), slog.LevelDebug) {
				hostinfo.logger(f.l).Debug("Failed to verify relay packet", "error", err, "from", via, "header", h)
			}
			return
		}
		f.handleOutsideRelayPacket(hostinfo, via, out, packet, h, fwPacket, lhf, nb, q, localCache)
		return
	}

	out, err = hostinfo.ConnectionState.Decrypt(f.l, h.MessageCounter, out, packet, nb)
	if err != nil {
		if f.l.Enabled(context.Background(), slog.LevelDebug) {
			hostinfo.logger(f.l).Debug("Failed to decrypt packet", "error", err, "from", via, "header", h)
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

**File:** e2e/tunnels_test.go (L471-473)
```go
func TestCloseTunnelAuthenticated(t *testing.T) {
	t.Parallel()
	ca, _, caKey, _ := cert_test.NewTestCaCert(cert.Version1, cert.Curve_CURVE25519, time.Now(), time.Now().Add(10*time.Minute), nil, nil, []string{})
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

**File:** CHANGELOG.md (L188-191)
```markdown
### Changed

- Disable sending `recv_error` messages when a packet is received outside the allowable counter window. (#1459)
- Improve error messages and remove some unnecessary fatal conditions in the Windows and generic udp listener. (#1453)
```
