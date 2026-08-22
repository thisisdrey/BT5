## Title
Unauthenticated `RecvError` packet allows remote tunnel-teardown DoS via spoofable/weak remote-address check - (File: outside.go)

## Summary
The external report describes `cancel_self_service_request` deleting another shareholder's pending request because it fails to verify the caller actually owns the target request, relying only on a lookup by ID rather than a strong ownership/authorization check. The nebula analog is `handleRecvError` in `outside.go`, which processes an unauthenticated, unencrypted `header.RecvError` packet and tears down a peer's live tunnel based only on a weak, sometimes-bypassable check tying the packet to the target `HostInfo`.

## Finding Description
`RecvError` packets are handled in the "Unencrypted packets" branch of `readOutsidePackets`, before any decryption or Noise/cert-based authentication occurs: [1](#0-0) 

The handler resolves a hostinfo purely from the attacker-controlled `RemoteIndex` field in the packet header and then only checks whether the hostinfo's currently known remote address matches the packet's source address — and only if that stored remote is itself "valid": [2](#0-1) 

This mirrors the flaw pattern in the report: the code looks up a target record by an ID supplied by the actor and performs deletion/state-mutation logic without a strong cryptographic binding proving the actor is authorized to act on that record. Here, `RemoteIndex` is a 32-bit locally generated index (not secret, and observable to any relayed peer, on-path observer, or via race/roaming windows), and the identity check is an IP:port address comparison rather than a Noise/cert-derived authentication tag. Critically, when `hostinfo.GetRemote()` is not yet "valid" (e.g., during handshake/roaming races or in certain relay-adjacent hostinfo states), the address check is skipped entirely (`hr.IsValid() && hr != addr`), and the packet is accepted unconditionally, causing `f.closeTunnel(hostinfo)` and `f.handshakeManager.DeleteHostInfo(hostinfo)` to run — deleting the peer's tunnel/pending-hostmap entry with no cryptographic proof of authorization, exactly analogous to `cancel_self_service_request` deleting a shareholder's request without verifying ownership.

By contrast, the codebase explicitly protects the authenticated `CloseTunnel` message type by requiring it to pass Noise-derived AEAD decryption first (see the `header.CloseTunnel` case, reached only after `hostinfo.ConnectionState.Decrypt` succeeds), and this is validated by `TestCloseTunnelAuthenticated`, which shows a bogus, unauthenticated `CloseTunnel` packet is correctly rejected. `RecvError`, however, has no equivalent authentication requirement, so a spoofed/guessed-index `RecvError` still succeeds in tearing down state, unlike `CloseTunnel`.

## Impact Explanation
An attacker with no valid CA-signed certificate for the target relationship can, by guessing or observing a victim's `RemoteIndex` value (or by acting as a relay/on-path observer that can see indexes in cleartext headers), forge `RecvError` packets that: (1) close an established tunnel via `f.closeTunnel(hostinfo)`, and (2) delete pending handshake state via `hm.DeleteHostInfo`. This is a remote, unauthenticated denial-of-service against victim tunnels/handshakes, directly analogous to the "malicious shareholder deletes other shareholders' pending requests" DoS in the report, since neither ownership nor a strong cryptographic identity check gates the deletion.

## Likelihood Explanation
Exploitability depends on knowing/guessing the 32-bit `RemoteIndex`, which is not secret by design (it appears in cleartext handshake/message headers and is visible to relays and on-path attackers), and on hitting the window where `hr.IsValid()` is false (pending/racing hostinfo) or where the attacker can also spoof the matching source address. This is a "Medium" likelihood analog — it requires either an on-path/relay position or a timing/race window, similar to the report's "Medium/Timing" difficulty rating for the original bug.

## Recommendation
- Require `RecvError` to be tied to a value the attacker cannot forge or observe (e.g., authenticate `RecvError` using the connection's Noise-derived keys instead of processing it as a cleartext/unauthenticated packet, similar to how `CloseTunnel` is gated behind `ConnectionState.Decrypt`).
- Do not skip the source-address check when `hr.IsValid()` is false; instead, treat an invalid/absent remote as "cannot verify," and refuse to act (or require additional confirmation) rather than defaulting to accept.
- Add negative tests specifically for spoofed `RecvError` packets analogous to `TestCloseTunnelAuthenticated`, verifying that a forged `RecvError` from a non-matching or unverified address cannot delete a peer's hostinfo.

## Proof of Concept
1. Establish or observe an in-progress tunnel/handshake between two nebula nodes so that a `RemoteIndex` value for the victim's `HostInfo` becomes known or guessable to the attacker (e.g., attacker acts as/observes a relay, or races the handshake window before `SetRemote` completes so `hr.IsValid()` is false).
2. From an arbitrary UDP source (no valid cert, no completed handshake with the victim needed), craft a bare header with `Type = header.RecvError` and `RemoteIndex` set to the victim's index, and send it to the victim node's listen port — this path is reached in `readOutsidePackets` before any Noise/cert authentication (`outside.go:81-84`).
3. On the victim, `handleRecvError` (`outside.go:541-575`) resolves the hostinfo by `RemoteIndex` via `QueryReverseIndex`; if `hr.IsValid()` is false for that hostinfo (or the attacker also spoofs the matching source `AddrPort`), the spoof check is bypassed and `f.closeTunnel(hostinfo)` plus `hm.DeleteHostInfo` execute, tearing down the victim's tunnel/pending handshake state without any cryptographic proof that the attacker is the legitimate peer.

Note: I was not able to fully trace every code path that can leave `hostinfo.GetRemote()` in an "invalid" state (e.g., all relay/roaming edge cases) within the available index; a full confirmation of the exact reachable window for bypassing the address check would benefit from deeper review of `remote_list.go` and `hostmap.go`'s `SetRemote`/`GetRemote` implementations, and of `acceptRecvErrorConfig.ShouldRecvError` rate-limiting semantics in `interface.go`, which I could not inspect in this session due to tool-call limits. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

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

**File:** e2e/tunnels_test.go (L471-573)
```go
func TestCloseTunnelAuthenticated(t *testing.T) {
	t.Parallel()
	ca, _, caKey, _ := cert_test.NewTestCaCert(cert.Version1, cert.Curve_CURVE25519, time.Now(), time.Now().Add(10*time.Minute), nil, nil, []string{})
	myControl, myVpnIpNet, myUdpAddr, _ := newSimpleServer(cert.Version1, ca, caKey, "me", "10.128.0.1/24", m{"tunnels": m{"drop_inactive": true, "inactivity_timeout": "5s"}})
	theirControl, theirVpnIpNet, theirUdpAddr, _ := newSimpleServer(cert.Version1, ca, caKey, "them", "10.128.0.2/24", m{"tunnels": m{"drop_inactive": true, "inactivity_timeout": "10m"}})

	// Share our underlay information
	myControl.InjectLightHouseAddr(theirVpnIpNet[0].Addr(), theirUdpAddr)
	theirControl.InjectLightHouseAddr(myVpnIpNet[0].Addr(), myUdpAddr)

	// Start the servers
	myControl.Start()
	theirControl.Start()

	r := router.NewR(t, myControl, theirControl)

	r.Log("Assert the tunnel between me and them works")
	assertTunnel(t, myVpnIpNet[0].Addr(), theirVpnIpNet[0].Addr(), myControl, theirControl, r)

	r.Log("Close the tunnel")
	myControl.CloseTunnel(theirVpnIpNet[0].Addr(), false)
	r.FlushAll()

	waitStart := time.Now()
	for {
		myIndexes := myControl.GetHostmapIndexCount()
		theirIndexes := theirControl.GetHostmapIndexCount()
		if myIndexes == 0 && theirIndexes == 0 {
			break
		}

		since := time.Since(waitStart)
		r.Logf("my tunnels: %v; their tunnels: %v; duration: %v", myIndexes, theirIndexes, since)
		if since > time.Second*6 {
			t.Fatal("Tunnel should have been declared inactive after 2 seconds and before 6 seconds")
		}

		time.Sleep(1 * time.Second)
		//r.FlushAll()
	}

	r.Logf("Happy path success, tunnels were dropped within %v", time.Since(waitStart))

	myControl.InjectLightHouseAddr(theirVpnIpNet[0].Addr(), theirUdpAddr)
	theirControl.InjectLightHouseAddr(myVpnIpNet[0].Addr(), myUdpAddr)
	r.Log("Assert another tunnel between me and them works")
	assertTunnel(t, myVpnIpNet[0].Addr(), theirVpnIpNet[0].Addr(), myControl, theirControl, r)
	hi := myControl.GetHostInfoByVpnAddr(theirVpnIpNet[0].Addr(), false)
	if hi == nil {
		t.Fatal("There is no hostinfo for this tunnel")
	}
	myHi := theirControl.GetHostInfoByVpnAddr(myVpnIpNet[0].Addr(), false)
	if myHi == nil {
		t.Fatal("There is no hostinfo for my tunnel")
	}
	r.Log("It does")

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

		since := time.Since(waitStart)
		r.Logf("my tunnels: %v; their tunnels: %v; duration: %v", myIndexes, theirIndexes, since)
		if since > time.Second*4 {
			t.Log("The tunnel would have been gone by now")
			break
		}

		time.Sleep(1 * time.Second)
		r.FlushAll()
	}

	myControl.Stop()
	theirControl.Stop()
}
```
