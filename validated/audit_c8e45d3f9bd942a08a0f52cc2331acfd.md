### Title
Handshake stage-1 (msg1) response is bound to whoever forwards the captured packet first, letting an unauthenticated attacker hijack/deny a legitimate initiator's handshake - ([File: handshake_manager.go])

### Summary
The external report describes a permit-based swap where the contract never checks that `msg.sender` actually owns the permit it is presented with, so an observer of the mempool can "front-run" a legitimate user's authorization data and cause the real user's transaction to revert. Nebula's stage-1 handshake path has the same class of gap at the transport layer: `beginHandshake` accepts any first-seen packet whose bytes match a legitimate initiator's stage-1 message and immediately binds the pending tunnel's remote address to `via.UdpAddr` — the network path the packet arrived on — without any way to prove that this network path is actually controlled by the certificate holder embedded in that message. An attacker who merely observes/relays a legitimate initiator's already-broadcast (but un-encrypted, since it is the handshake itself) stage-1 packet can get themselves bound as the pending remote for that handshake attempt.

### Finding Description
`HandshakeManager.HandleIncoming` routes any packet with `MessageCounter == 1` and `RemoteIndex == 0` straight to `beginHandshake`, with no verification tying the packet to the network address it should have come from: [1](#0-0) 

`beginHandshake` then runs the full noise machine over the packet and, on success, unconditionally sets the new pending `HostInfo`'s remote to `via.UdpAddr` — the address the packet was received *from*, not an address that has been authenticated in any way at this point: [2](#0-1) 

The stage-2 response is then sent to that same `via.UdpAddr`: [3](#0-2) 

Because the noise handshake bytes for stage 1 are exactly what the legitimate initiator sends over the wire (this is the un-authenticated first leg of the protocol; that is precisely why it exists), anyone who can observe or intercept that packet in flight can copy/replay it verbatim from a different source address (e.g., a spoofed UDP source, or by simply forwarding it faster than the original packet's path) before the genuine copy reaches the responder. If the attacker's copy is processed first, `CheckAndComplete` adds this `HostInfo` to the responder's pending map, bound to the attacker's address: [4](#0-3) 

When the legitimate initiator's own (identical-byte) packet subsequently arrives, it is treated purely as a duplicate: `CheckAndComplete` matches it via byte equality and returns `ErrAlreadySeen`, and the manager only re-preferences the remote if `SetRemoteIfPreferred` decides the new sender's address is "preferred" over the one already bound: [5](#0-4) [6](#0-5) 

If the genuine initiator's address is not in a configured `preferred_ranges` entry, `SetRemoteIfPreferred` returns `false` and the cached stage-2 response keeps going to the attacker's address instead of the legitimate initiator's. Since the stage-2 payload is cryptographically bound to the initiator's own ephemeral key, the attacker cannot actually complete the handshake or decrypt traffic — but the legitimate initiator never receives the response either, so their handshake attempt simply times out, exactly mirroring the reported bug class: the credential-bearing message (permit / handshake msg1) is usable by anyone who can present or replay it first, and the rightful owner's operation is starved/reverted as a result.

### Impact Explanation
This is a remote, unauthenticated denial-of-service against the handshake establishment process: an attacker with no valid Nebula certificate can suppress or delay a legitimate node's ability to establish a tunnel with a target by winning the race to have their forwarded/spoofed copy of the victim's own stage-1 packet processed first. It does not grant the attacker any decrypted traffic or an established tunnel (the crypto still binds the response to the true initiator's ephemeral key), but it can be used to selectively block specific initiator→responder tunnel establishments, delaying connectivity and forcing repeated handshake retries — a remote-crash/remote-state-poisoning-adjacent DoS against the handshake manager's pending-hostinfo state.

### Likelihood Explanation
Exploitability depends on network position: the attacker needs to observe (or already be on-path for) the victim's outbound stage-1 UDP packet and be able to deliver a copy to the responder before the genuine packet arrives, optionally with a spoofed source address if UDP source spoofing is not filtered by the network. This is a realistic scenario for on-path attackers, malicious relay/lighthouse-adjacent network operators, or ISPs, but not for a fully passive off-path attacker with no view of the victim's traffic. Likelihood is moderate — it requires some networking capability beyond simply holding no certificate, but it does not require compromising cryptographic material.

### Recommendation
Do not treat the first-seen source address of a stage-1 packet as authoritative for a pending handshake before the responder has any cryptographic assurance about that path. Concretely:
- Do not call `hostinfo.SetRemote(via.UdpAddr)` in `beginHandshake` before the handshake is cryptographically complete; defer binding the "preferred"/authoritative remote until the responder has validated the peer certificate (as already happens later in `continueHandshake`/`validatePeerCert`).
- When a duplicate (`ErrAlreadySeen`) stage-1 packet arrives from a different address, prefer re-sending the response to all recently seen candidate addresses (or explicitly re-race) rather than favoring only the first, unauthenticated sender.
- Consider using `preferred_ranges`-independent tie-breaking (e.g. don't let an unauthenticated first-seen address "own" the pending slot) so that a legitimate initiator's retransmission can reclaim the cached response path even when its address is not in a preferred range.

### Proof of Concept
1. Node `me` has no route to `them` yet and triggers a handshake, sending stage-1 msg1 to `them`'s address.
2. An on-path/off-path relaying attacker captures this exact UDP payload and immediately forwards a copy to `them` from a different source address (`evil-addr`) before `me`'s own packet arrives (achievable via a faster path or by racing retransmissions, and easier still if UDP source-address spoofing of `me`'s original packet is not filtered).
3. `them.beginHandshake` processes the attacker's copy first: it runs the full noise machine successfully (bytes are identical to a legitimate msg1), calls `CheckAndComplete` (no existing entry, so no error), and binds the new pending `HostInfo.SetRemote(evil-addr)`, then sends the stage-2 response to `evil-addr`.
4. `me`'s genuine packet arrives shortly after with identical bytes; `CheckAndComplete` returns `ErrAlreadySeen`, and unless `me`'s real address happens to fall in a `preferred_ranges` entry, `SetRemoteIfPreferred` returns `false`, so the cached stage-2 response is (re)sent to `evil-addr`, not to `me`.
5. `me` never receives a stage-2 response, its handshake attempt times out via `HandshakeManager.handleOutbound`'s retry/timeout logic, and `me` must retry the handshake from scratch — mirroring the reported "user receives a revert and must rebuild without the permit" outcome.

Note: I was not able to execute or fully trace runtime timing/race conditions (e.g., exact retransmission cadence, `preferred_ranges` defaults, or whether other transport-level protections such as ingress filtering are assumed by the threat model) using static analysis alone; a live e2e reproduction (similar to the existing `TestWrongResponderHandshake`/`TestHandshakeRetransmitDuplicate` tests in `e2e/handshakes_test.go` and `e2e/handshake_manager_test.go`) would be needed to confirm the exact race window and `SetRemoteIfPreferred` behavior in practice. [7](#0-6) [8](#0-7)

### Citations

**File:** handshake_manager.go (L172-185)
```go
	// First message of a new handshake. The wire format requires RemoteIndex
	// to be zero here (the initiator has no responder index to fill in yet),
	// and generateIndex never allocates 0, so any non-zero RemoteIndex on a
	// stage-1 packet is malformed or someone probing for an index collision.
	// Drop without paying the cost of running noise on a pending Machine.
	if h.MessageCounter == 1 {
		if h.RemoteIndex != 0 {
			hm.l.Debug("dropping stage-1 handshake with non-zero RemoteIndex",
				"from", via, "remoteIndex", h.RemoteIndex)
			return
		}
		hm.beginHandshake(via, packet, h)
		return
	}
```

**File:** handshake_manager.go (L430-477)
```go
func (hm *HandshakeManager) CheckAndComplete(hostinfo *HostInfo, handshakePacket uint8, f *Interface) (*HostInfo, error) {
	hm.mainHostMap.Lock()
	defer hm.mainHostMap.Unlock()
	hm.Lock()
	defer hm.Unlock()

	// Check if we already have a tunnel with this vpn ip
	existingHostInfo, found := hm.mainHostMap.Hosts[hostinfo.vpnAddrs[0]]
	if found && existingHostInfo != nil {
		// Is it just a delayed handshake packet? Check every hostinfo we hold for this address.
		for _, testHostInfo := range hm.mainHostMap.unlockedGetHostList(hostinfo.vpnAddrs[0]) {
			if bytes.Equal(hostinfo.HandshakePacket[handshakePacket], testHostInfo.HandshakePacket[handshakePacket]) {
				return testHostInfo, ErrAlreadySeen
			}
		}

		// Is this a newer handshake?
		if existingHostInfo.lastHandshakeTime >= hostinfo.lastHandshakeTime && !existingHostInfo.ConnectionState.initiator {
			return existingHostInfo, ErrExistingHostInfo
		}

		existingHostInfo.logger(hm.l).Info("Taking new handshake")
	}

	existingIndex, found := hm.mainHostMap.Indexes[hostinfo.localIndexId]
	if found {
		// We have a collision, but for a different hostinfo
		return existingIndex, ErrLocalIndexCollision
	}

	existingPendingIndex, found := hm.indexes[hostinfo.localIndexId]
	if found && existingPendingIndex.hostinfo != hostinfo {
		// We have a collision, but for a different hostinfo
		return existingPendingIndex.hostinfo, ErrLocalIndexCollision
	}

	existingRemoteIndex, found := hm.mainHostMap.RemoteIndexes[hostinfo.remoteIndexId]
	if found && existingRemoteIndex != nil && existingRemoteIndex.vpnAddrs[0] != hostinfo.vpnAddrs[0] {
		// We have a collision, but this can happen since we can't control
		// the remote ID. Just log about the situation as a note.
		hostinfo.logger(hm.l).Info("New host shadows existing host remoteIndex",
			"collision", existingRemoteIndex.vpnAddrs,
		)
	}

	hm.mainHostMap.unlockedAddHostInfo(hostinfo, f)
	return existingHostInfo, nil
}
```

**File:** handshake_manager.go (L791-795)
```go
	hostinfo.remotes = f.lightHouse.QueryCache(vpnAddrs)
	if !via.IsRelayed {
		hostinfo.SetRemote(via.UdpAddr)
	}
	hostinfo.buildNetworks(f.myVpnNetworksTable, remoteCert.Certificate)
```

**File:** handshake_manager.go (L797-803)
```go
	existing, err := hm.CheckAndComplete(hostinfo, handshakePacketStage0, f)
	if err != nil {
		hm.handleCheckAndCompleteError(err, existing, hostinfo, via)
		return
	}

	hm.sendHandshakeResponse(via, response, hostinfo, false)
```

**File:** handshake_manager.go (L1104-1113)
```go
	case ErrAlreadySeen:
		if existing.SetRemoteIfPreferred(f.hostMap, via) {
			f.SendMessageToVpnAddr(header.Test, header.TestRequest, hostinfo.vpnAddrs[0], []byte(""), make([]byte, 12, 12), make([]byte, mtu))
		}
		// Resend the original response. The peer is committed to that response's
		// ephemeral keys; a freshly-built one would have different keys and break
		// the tunnel even though both sides "completed" the handshake.
		if msg := existing.HandshakePacket[handshakePacketStage2]; msg != nil {
			hm.sendHandshakeResponse(via, msg, existing, true)
		}
```

**File:** hostmap.go (L785-823)
```go
// SetRemoteIfPreferred returns true if the remote was changed. The lastRoam
// time on the HostInfo will also be updated.
func (i *HostInfo) SetRemoteIfPreferred(hm *HostMap, via ViaSender) bool {
	if via.IsRelayed {
		return false
	}

	currentRemote := i.GetRemote()
	if !currentRemote.IsValid() {
		i.SetRemote(via.UdpAddr)
		return true
	}

	// NOTE: We do this loop here instead of calling `isPreferred` in
	// remote_list.go so that we only have to loop over preferredRanges once.
	newIsPreferred := false
	for _, l := range hm.GetPreferredRanges() {
		// return early if we are already on a preferred remote
		if l.Contains(currentRemote.Addr()) {
			return false
		}

		if l.Contains(via.UdpAddr.Addr()) {
			newIsPreferred = true
		}
	}

	if newIsPreferred {
		// Consider this a roaming event
		i.lastRoam = time.Now()
		i.lastRoamRemote = currentRemote

		i.SetRemote(via.UdpAddr)

		return true
	}

	return false
}
```

**File:** e2e/handshake_manager_test.go (L30-67)
```go
func TestHandshakeRetransmitDuplicate(t *testing.T) {
	t.Parallel()
	// Verify the responder correctly handles receiving the same msg1 multiple times
	// (retransmission). The duplicate goes through CheckAndComplete -> ErrAlreadySeen
	// and the cached response is resent.

	ca, _, caKey, _ := cert_test.NewTestCaCert(cert.Version1, cert.Curve_CURVE25519, time.Now(), time.Now().Add(10*time.Minute), nil, nil, []string{})
	myControl, myVpnIpNet, myUdpAddr, _ := newSimpleServer(cert.Version1, ca, caKey, "me", "10.128.0.1/24", nil)
	theirControl, theirVpnIpNet, theirUdpAddr, _ := newSimpleServer(cert.Version1, ca, caKey, "them", "10.128.0.2/24", nil)

	myControl.InjectLightHouseAddr(theirVpnIpNet[0].Addr(), theirUdpAddr)
	theirControl.InjectLightHouseAddr(myVpnIpNet[0].Addr(), myUdpAddr)

	myControl.Start()
	theirControl.Start()

	r := router.NewR(t, myControl, theirControl)
	defer r.RenderFlow()

	t.Log("Trigger handshake from me to them")
	myControl.InjectTunPacket(BuildTunUDPPacket(theirVpnIpNet[0].Addr(), 80, myVpnIpNet[0].Addr(), 80, []byte("Hi")))

	t.Log("Grab my msg1")
	msg1 := myControl.GetFromUDP(true)

	t.Log("Inject msg1 into them, first time")
	theirControl.InjectUDPPacket(msg1)
	_ = theirControl.GetFromUDP(true)

	t.Log("Inject the SAME msg1 again, tests ErrAlreadySeen path")
	theirControl.InjectUDPPacket(msg1)
	resp2 := theirControl.GetFromUDP(true)
	assert.NotNil(t, resp2, "should get cached response on duplicate msg1")

	t.Log("Complete handshake with cached response")
	myControl.InjectUDPPacket(resp2)
	myControl.WaitForType(1, 0, theirControl)

```

**File:** e2e/handshakes_test.go (L189-264)
```go
func TestWrongResponderHandshake(t *testing.T) {
	t.Parallel()
	ca, _, caKey, _ := cert_test.NewTestCaCert(cert.Version1, cert.Curve_CURVE25519, time.Now(), time.Now().Add(10*time.Minute), nil, nil, []string{})

	myControl, myVpnIpNet, myUdpAddr, _ := newSimpleServer(cert.Version1, ca, caKey, "me", "10.128.0.100/24", nil)
	theirControl, theirVpnIpNet, theirUdpAddr, _ := newSimpleServer(cert.Version1, ca, caKey, "them", "10.128.0.99/24", nil)
	evilControl, evilVpnIp, evilUdpAddr, _ := newSimpleServer(cert.Version1, ca, caKey, "evil", "10.128.0.2/24", nil)

	// Put the evil udp addr in for their vpn Ip, this is a case of being lied to by the lighthouse.
	myControl.InjectLightHouseAddr(theirVpnIpNet[0].Addr(), evilUdpAddr)

	// Build a router so we don't have to reason who gets which packet
	r := router.NewR(t, myControl, theirControl, evilControl)
	defer r.RenderFlow()

	// Start the servers
	myControl.Start()
	theirControl.Start()
	evilControl.Start()

	t.Log("Start the handshake process, we will route until we see the evil tunnel closed")
	myControl.InjectTunPacket(BuildTunUDPPacket(theirVpnIpNet[0].Addr(), 80, myVpnIpNet[0].Addr(), 80, []byte("Hi from me")))

	h := &header.H{}
	r.RouteForAllExitFunc(func(p *udp.Packet, c *nebula.Control) router.ExitType {
		err := h.Parse(p.Data)
		if err != nil {
			panic(err)
		}

		if h.Type == header.CloseTunnel && p.To == evilUdpAddr {
			return router.RouteAndExit
		}

		return router.KeepRouting
	})

	t.Log("Evil tunnel is closed, inject the correct udp addr for them")
	myControl.InjectLightHouseAddr(theirVpnIpNet[0].Addr(), theirUdpAddr)
	pendingHi := myControl.GetHostInfoByVpnAddr(theirVpnIpNet[0].Addr(), true)
	assert.NotContains(t, pendingHi.RemoteAddrs, evilUdpAddr)

	t.Log("Route until we see the cached packet")
	r.RouteForAllExitFunc(func(p *udp.Packet, c *nebula.Control) router.ExitType {
		err := h.Parse(p.Data)
		if err != nil {
			panic(err)
		}

		if p.To == theirUdpAddr && h.Type == 1 {
			return router.RouteAndExit
		}

		return router.KeepRouting
	})

	t.Log("My cached packet should be received by them")
	myCachedPacket := theirControl.GetFromTun(true)
	assertUdpPacket(t, []byte("Hi from me"), myCachedPacket, myVpnIpNet[0].Addr(), theirVpnIpNet[0].Addr(), 80, 80)

	t.Log("Test the tunnel with them")
	assertHostInfoPair(t, myUdpAddr, theirUdpAddr, myVpnIpNet, theirVpnIpNet, myControl, theirControl)
	assertTunnel(t, myVpnIpNet[0].Addr(), theirVpnIpNet[0].Addr(), myControl, theirControl, r)

	t.Log("Flush all packets from all controllers")
	r.FlushAll()

	t.Log("Ensure ensure I don't have any hostinfo artifacts from evil")
	assert.Nil(t, myControl.GetHostInfoByVpnAddr(evilVpnIp[0].Addr(), true), "My pending hostmap should not contain evil")
	assert.Nil(t, myControl.GetHostInfoByVpnAddr(evilVpnIp[0].Addr(), false), "My main hostmap should not contain evil")

	r.RenderHostmaps("Final hostmaps", myControl, theirControl, evilControl)
	t.Log("Success!")
	myControl.Stop()
	theirControl.Stop()
}
```
