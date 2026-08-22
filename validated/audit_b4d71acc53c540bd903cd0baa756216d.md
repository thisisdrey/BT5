Found it: `RemoteList.badRemotes` (`remote_list.go:217`) grows unbounded via `BlockRemote()` [1](#0-0) , and every entry is checked with a linear `slices.Contains` scan in `unlockedIsBad()` on every `Rebuild()`/`ForEach()` call in the handshake hot path [2](#0-1) . This is directly analogous to the reported "unbounded shareholders" DoS: an attacker who repeatedly answers a victim's lighthouse-directed handshake with the wrong VPN address (impersonating whichever host the victim is told to reach) causes `continueHandshake` to call `hostinfo.remotes.BlockRemote(via)` on every failed attempt [3](#0-2) , appending to `badRemotes` with no cap, unlike the sibling `reported`/`relay` lists which are explicitly bounded by `MaxRemotes` [4](#0-3) .

### Title
Unbounded `RemoteList.badRemotes` growth via repeated wrong-responder handshakes causes CPU/O(n) DoS on the handshake hot path - (File: `remote_list.go`)

### Summary
`RemoteList` deduplicates and filters underlay addresses using a `badRemotes` slice that records addresses that answered a handshake for the wrong VPN identity [5](#0-4) . Unlike the `reported`/`relay` caches, which are explicitly capped at `MaxRemotes` (10) before being copied into the `RemoteList` [4](#0-3) [6](#0-5) , `badRemotes` has no bound: `BlockRemote()` appends a new entry every time it is called and is only reset on `RefreshFromHandshake` or explicit `ResetBlockedRemotes` [7](#0-6) .

### Finding Description
`BlockRemote` is invoked from the handshake manager whenever a peer responds to a handshake attempt with a certificate for the wrong VPN address ("incorrect host responded to handshake") [8](#0-7) . The check for duplicate entries before appending, `unlockedIsBad`, is itself an O(n) linear scan (`slices.Contains(r.badRemotes, remote)`) over the same unbounded slice [2](#0-1) , and this same function is called from `unlockedCollect()` for every address in the reported/learned caches on every `Rebuild()` [9](#0-8) , and `Rebuild()` itself is invoked on every `ForEach`/`Len` call, i.e., on every handshake retry tick in `handleOutbound` [10](#0-9) [11](#0-10) .

An attacker does not need a CA-signed certificate matching the victim's target peer to trigger this: they only need to be reachable at whatever underlay address the victim's lighthouse (or static host map, or another attacker-controlled lighthouse reply) points the victim's outbound handshake at, and respond with *any* valid certificate (potentially their own, signed by the same CA the victim trusts, or via a manipulated `HostQueryReply`) that does not match the vpnAddr the victim intended. Each such wrong-responder round trip appends one more entry to `badRemotes` and is never capped. Because `unlockedIsBad` and `unlockedCollect` are O(n) in the size of this list and are on the hot path of essentially every future handshake attempt and remote-list read for that pending hostinfo, repeated wrong-responder handshakes accumulate cost that grows unbounded per additional bad entry.

### Impact Explanation
While a single tunnel's `badRemotes` list is scoped to one `RemoteList`/`HostInfo` and reset on successful handshake completion (`RefreshFromHandshake`), an attacker who can repeatedly cause wrong-responder handshakes for a pending tunnel (e.g., by controlling a lighthouse reply or racing address updates) before the handshake ever completes can drive the list arbitrarily large, degrading `Rebuild`/`ForEach`/`unlockedIsBad` calls that run on every handshake attempt and firewall/data-path read of that RemoteList, causing a CPU-exhaustion style DoS on the handshake retry loop for that peer — mirroring the "unbounded number of shareholders" pattern where an attacker-influenced unbounded list is iterated on every hot-path operation with no cap set.

### Likelihood Explanation
Reaching this requires the ability to make the victim believe an attacker-controlled (or spoofed) underlay address belongs to the peer it's trying to reach, i.e., control or race a lighthouse/`HostQueryReply` answer or a static-host-map/relay-learned entry — the codebase's own tests (`TestHandshakeWrongResponderPacketStore`, `TestWrongResponderHandshake`) explicitly exercise this "lied to by the lighthouse" scenario as a normal recoverable condition [12](#0-11) , showing it is a reachable, expected code path rather than a theoretical corner case. Achieving unbounded growth would require the attacker to repeat this multiple times for the same pending handshake, which is rate-limited only by the handshake retry interval/backoff, not by any cap on `badRemotes` itself.

### Recommendation
Cap `badRemotes` similarly to `MaxRemotes`/`reported`/`relay` (e.g., cap size and/or use a set/map keyed structure instead of a linearly-scanned slice), and consider evicting oldest entries once the cap is reached, consistent with the bounding already applied to `unlockedSetV4`/`unlockedSetV6`/`unlockedSetRelay` in the same file [13](#0-12) .

### Proof of Concept
1. Victim initiates a handshake toward `theirVpnIp`, guided by a lighthouse entry or static host map that an attacker can influence to repeatedly point at attacker-controlled underlay addresses.
2. Attacker responds to the victim's stage-1 handshake message with a valid, CA-signed certificate for a *different* VPN address than the one requested.
3. `continueHandshake` detects `!correctHostResponded`, deletes the pending hostinfo, restarts the handshake, and calls `hostinfo.remotes.BlockRemote(via)`, appending the attacker's address to `badRemotes` [8](#0-7) .
4. Attacker repeats step 2 from many distinct source addresses/ports (or replays across repeated lighthouse-driven retries) before the victim ever completes a real handshake with the intended peer, growing `badRemotes` without bound.
5. Each subsequent `Rebuild()`/`ForEach()`/`unlockedIsBad()` call for that `RemoteList` — invoked on every handshake retry tick — now scans the ever-growing `badRemotes` slice, degrading handshake processing for that peer.

### Citations

**File:** remote_list.go (L215-217)
```go
	// This is a list of remotes that we have tried to handshake with and have returned from the wrong vpn ip.
	// They should not be tried again during a handshake
	badRemotes []netip.AddrPort
```

**File:** remote_list.go (L267-284)
```go
// Len locks and reports the size of the deduplicated address list
// The deduplication work may need to occur here, so you must pass preferredRanges
func (r *RemoteList) Len(preferredRanges []netip.Prefix) int {
	r.Rebuild(preferredRanges)
	r.RLock()
	defer r.RUnlock()
	return len(r.addrs)
}

// ForEach locks and will call the forEachFunc for every deduplicated address in the list
// The deduplication work may need to occur here, so you must pass preferredRanges
func (r *RemoteList) ForEach(preferredRanges []netip.Prefix, forEach forEachFunc) {
	r.Rebuild(preferredRanges)
	r.RLock()
	for _, v := range r.addrs {
		forEach(v, isPreferred(v.Addr(), preferredRanges))
	}
	r.RUnlock()
```

**File:** remote_list.go (L377-424)
```go
// BlockRemote locks and records the address as bad, it will be excluded from the deduplicated address list
func (r *RemoteList) BlockRemote(bad ViaSender) {
	if bad.IsRelayed {
		return
	}

	r.Lock()
	defer r.Unlock()

	// Check if we already blocked this addr
	if r.unlockedIsBad(bad.UdpAddr) {
		return
	}

	// We copy here because we are taking something else's memory and we can't trust everything
	r.badRemotes = append(r.badRemotes, bad.UdpAddr)

	// Mark the next interaction must recollect/dedupe
	r.shouldRebuild = true
}

// CopyBlockedRemotes locks and makes a deep copy of the blocked remotes list
func (r *RemoteList) CopyBlockedRemotes() []netip.AddrPort {
	r.RLock()
	defer r.RUnlock()

	c := make([]netip.AddrPort, len(r.badRemotes))
	for i, v := range r.badRemotes {
		c[i] = v
	}
	return c
}

// RefreshFromHandshake locks and updates the RemoteList to account for data learned upon a completed handshake
func (r *RemoteList) RefreshFromHandshake(vpnAddrs []netip.Addr) {
	r.Lock()
	r.badRemotes = nil
	r.vpnAddrs = make([]netip.Addr, len(vpnAddrs))
	copy(r.vpnAddrs, vpnAddrs)
	r.Unlock()
}

// ResetBlockedRemotes locks and clears the blocked remotes list
func (r *RemoteList) ResetBlockedRemotes() {
	r.Lock()
	r.badRemotes = nil
	r.Unlock()
}
```

**File:** remote_list.go (L442-445)
```go
// unlockedIsBad assumes you have the write lock and checks if the remote matches any entry in the blocked address list
func (r *RemoteList) unlockedIsBad(remote netip.AddrPort) bool {
	return slices.Contains(r.badRemotes, remote)
}
```

**File:** remote_list.go (L454-480)
```go
// unlockedSetV4 assumes you have the write lock and resets the reported list of ips for this owner to the list provided
// and marks the deduplicated address list as dirty
func (r *RemoteList) unlockedSetV4(ownerVpnIp, vpnIp netip.Addr, to []*V4AddrPort, check checkFuncV4) {
	r.shouldRebuild = true
	c := r.unlockedGetOrMakeV4(ownerVpnIp)

	// Reset the slice
	c.reported = c.reported[:0]

	// We can't take their array but we can take their pointers
	for _, v := range to[:minInt(len(to), MaxRemotes)] {
		if check(vpnIp, v) {
			c.reported = append(c.reported, v)
		}
	}
}

func (r *RemoteList) unlockedSetRelay(ownerVpnIp netip.Addr, to []netip.Addr) {
	r.shouldRebuild = true
	c := r.unlockedGetOrMakeRelay(ownerVpnIp)

	// Reset the slice
	c.relay = c.relay[:0]

	// We can't take their array but we can take their pointers
	c.relay = append(c.relay, to[:minInt(len(to), MaxRemotes)]...)
}
```

**File:** remote_list.go (L577-618)
```go
func (r *RemoteList) unlockedCollect() {
	addrs := r.addrs[:0]
	relays := r.relays[:0]

	for _, c := range r.cache {
		if c.v4 != nil {
			if c.v4.learned != nil {
				u := protoV4AddrPortToNetAddrPort(c.v4.learned)
				if !r.unlockedIsBad(u) {
					addrs = append(addrs, u)
				}
			}

			for _, v := range c.v4.reported {
				if v == nil {
					continue
				}
				u := protoV4AddrPortToNetAddrPort(v)
				if !r.unlockedIsBad(u) {
					addrs = append(addrs, u)
				}
			}
		}

		if c.v6 != nil {
			if c.v6.learned != nil {
				u := protoV6AddrPortToNetAddrPort(c.v6.learned)
				if !r.unlockedIsBad(u) {
					addrs = append(addrs, u)
				}
			}

			for _, v := range c.v6.reported {
				if v == nil {
					continue
				}
				u := protoV6AddrPortToNetAddrPort(v)
				if !r.unlockedIsBad(u) {
					addrs = append(addrs, u)
				}
			}
		}
```

**File:** handshake_manager.go (L270-294)
```go
	remotes := hostinfo.remotes.CopyAddrs(hm.mainHostMap.GetPreferredRanges())
	remotesHaveChanged := !slices.Equal(remotes, hh.lastRemotes)

	// We only care about a lighthouse trigger if we have new remotes to send to.
	// This is a very specific optimization for a fast lighthouse reply.
	if lighthouseTriggered && !remotesHaveChanged {
		// If we didn't return here a lighthouse could cause us to aggressively send handshakes
		return
	}

	hh.lastRemotes = remotes

	// This will generate a load of queries for hosts with only 1 ip
	// (such as ones registered to the lighthouse with only a private IP)
	// So we only do it one time after attempting 5 handshakes already.
	if len(remotes) <= 1 && hh.counter == 5 {
		// If we only have 1 remote it is highly likely our query raced with the other host registered within the lighthouse
		// Our vpnIp here has a tunnel with a lighthouse but has yet to send a host update packet there so we only know about
		// the learned public ip for them. Query again to short circuit the promotion counter
		hm.lightHouse.QueryServer(vpnIp)
	}

	// Send the handshake to all known ips, stage 2 takes care of assigning the hostinfo.remote based on the first to reply
	var sentTo []netip.AddrPort
	hostinfo.remotes.ForEach(hm.mainHostMap.GetPreferredRanges(), func(addr netip.AddrPort, _ bool) {
```

**File:** handshake_manager.go (L923-944)
```go
	if !correctHostResponded {
		f.l.Info("Incorrect host responded to handshake",
			"intendedVpnAddrs", hostinfo.vpnAddrs,
			"haveVpnNetworks", vpnNetworks,
			"from", via,
			"certName", remoteCert.Certificate.Name(),
			"certVersion", remoteCert.Certificate.Version(),
			"fingerprint", remoteCert.Fingerprint,
			"issuer", remoteCert.Certificate.Issuer(),
			"handshake", m{"stage": uint64(machine.MessageIndex()), "style": header.SubTypeName(header.Handshake, machine.Subtype())},
		)

		hm.DeleteHostInfo(hostinfo)
		hm.StartHandshake(hostinfo.vpnAddrs[0], func(newHH *HandshakeHostInfo) {
			newHH.hostinfo.remotes = hostinfo.remotes
			newHH.hostinfo.remotes.BlockRemote(via)
			newHH.packetStore = hh.packetStore
			hh.packetStore = []*cachedPacket{}
			hostinfo.vpnAddrs = vpnAddrs
			f.sendCloseTunnel(hostinfo)
		})
		return
```

**File:** hostmap.go (L27-27)
```go
const MaxRemotes = 10
```

**File:** e2e/handshakes_test.go (L189-230)
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

```
