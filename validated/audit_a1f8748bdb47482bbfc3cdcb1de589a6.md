### Title
Unauthenticated `RecvError` packet spoofing forces premature tunnel teardown - ([File: outside.go])

### Summary
The Malda report describes a class of bug where a resource ends up controlled/gated by a party that cannot be authenticated or bound to the intended recipient, so a failure path leaves the resource unrecoverable. The equivalent bug-class in Nebula is trusting an unauthenticated, cleartext control signal (`header.RecvError`) to trigger an unrecoverable state transition — tearing down an established, mutually-authenticated tunnel — without any cryptographic binding to the peer that holds the session.

### Finding Description
`RecvError` packets are handled in `readOutsidePackets` *before* any decryption or certificate verification takes place, in the same block as the (also unauthenticated) `Handshake` type: [1](#0-0) 

`handleRecvError` resolves the target `HostInfo` purely from the attacker-controlled `h.RemoteIndex` field in the cleartext header, then checks only that the packet's plaintext UDP source address matches the hostinfo's currently recorded remote address before tearing the tunnel down: [2](#0-1) 

Both of the values needed to forge this packet are attacker-obtainable without holding a CA-signed certificate:
- `RemoteIndex` is a 32-bit value exchanged in the (also unauthenticated/unencrypted) handshake header, and is visible to anyone who can observe the wire between two peers (e.g., an on-path or off-path attacker capable of UDP source spoofing), unlike `CloseTunnel`, which is only processed after successful `ConnectionState.Decrypt` and therefore requires possession of the derived session key: [3](#0-2) 
- The UDP source address check is a plaintext comparison with no cryptographic proof of origin; UDP source addresses are trivially spoofable at the IP layer, which is exactly the property the codebase engineers around elsewhere (see the dedicated `TestFirewall_ConntrackSourceSpoofingAcrossPeers` anti-spoofing test for the overlay firewall): [4](#0-3) 

There is a rate/allow-list guard (`acceptRecvErrorConfig.ShouldRecvError`), but it is a courtesy throttle, not a cryptographic authentication check, and does not verify that the sender possesses the tunnel's Noise session key or a CA-signed certificate: [5](#0-4) 

### Impact Explanation
An attacker with no CA-signed certificate — who can only observe or guess a peer's `RemoteIndex`/underlay address and spoof UDP packets — can force `closeTunnel` on an established, mutually-authenticated session: [6](#0-5) 
This is a remote state-poisoning / denial-of-service impact against live tunnels: legitimate, authenticated sessions can be unilaterally torn down by an unauthenticated third party, forcing repeated re-handshakes and degrading availability, similar in spirit to how the Malda bug let an unauthorized failure path (a refund with no recipient validation) irreversibly disrupt the intended state.

### Likelihood Explanation
Exploitability depends on the attacker's ability to (a) learn a valid `RemoteIndex` for a target tunnel and (b) spoof the peer's UDP source address, both of which are easier for an off-path/network-adjacent attacker than compromising the Noise handshake or forging a valid certificate. This is a materially different (and by design lower) bar than the encrypted `CloseTunnel` path, which the codebase's own tests (`TestCloseTunnelAuthenticated`) show is properly protected by requiring successful decryption.

### Recommendation
Bind `RecvError` handling to session-authenticated material instead of a bare cleartext index/address match — e.g., require the packet to be encrypted/MACed with the tunnel's session key (as `CloseTunnel` already is), or otherwise cryptographically prove that the sender previously held or shares state with the addressed index, before allowing it to tear down an active tunnel.

### Proof of Concept
Not executed; based on static code-path analysis. Conceptually: an attacker observes (or otherwise obtains) a target's UDP `RemoteIndex` for an active tunnel to peer B, then sends a raw UDP packet of type `header.RecvError` with that `RemoteIndex`, spoofing the source address to match B's current remote address as seen by the target — `outside.go`'s `handleRecvError` accepts it and calls `f.closeTunnel(hostinfo)`, tearing down the legitimate session without ever validating a certificate or decrypting anything.

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

**File:** outside.go (L126-166)
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

**File:** firewall_test.go (L919-978)
```go
func TestFirewall_ConntrackSourceSpoofingAcrossPeers(t *testing.T) {
	l := test.NewLoggerWithOutput(&bytes.Buffer{})

	myVpnNetworksTable := new(bart.Lite)
	myVpnNetworksTable.Insert(netip.MustParsePrefix("192.0.2.1/24"))

	owner := &dummyCert{
		name:     "owner",
		networks: []netip.Prefix{netip.MustParsePrefix("192.0.2.1/24")},
	}

	victim := &cert.CachedCertificate{
		Certificate: &dummyCert{
			name:     "victim",
			networks: []netip.Prefix{netip.MustParsePrefix("192.0.2.2/24")},
		},
	}
	victimHI := HostInfo{
		ConnectionState: &ConnectionState{peerCert: victim},
		vpnAddrs:        []netip.Addr{netip.MustParseAddr("192.0.2.2")},
	}
	victimHI.buildNetworks(myVpnNetworksTable, victim.Certificate)

	attacker := &cert.CachedCertificate{
		Certificate: &dummyCert{
			name:     "attacker",
			networks: []netip.Prefix{netip.MustParsePrefix("192.0.2.3/24")},
		},
	}
	attackerHI := HostInfo{
		ConnectionState: &ConnectionState{peerCert: attacker},
		vpnAddrs:        []netip.Addr{netip.MustParseAddr("192.0.2.3")},
	}
	attackerHI.buildNetworks(myVpnNetworksTable, attacker.Certificate)

	fw := NewFirewall(l, time.Second, time.Minute, time.Hour, owner)
	// Allow any inbound traffic that passes the cert / source-IP checks.
	require.NoError(t, fw.AddRule(true, firewall.ProtoAny, 0, 0, []string{"any"}, "", "", "", "", ""))
	cp := cert.NewCAPool()

	flow := firewall.Packet{
		LocalAddr:  netip.MustParseAddr("192.0.2.1"),
		RemoteAddr: netip.MustParseAddr("192.0.2.2"),
		LocalPort:  443,
		RemotePort: 55000,
		Protocol:   firewall.ProtoUDP,
	}

	require.NoError(t, fw.Drop(flow, true, &victimHI, cp, nil),
		"victim's own traffic from its own overlay IP must be allowed")

	unseen := flow
	unseen.RemotePort = 55001
	assert.Equal(t, ErrInvalidRemoteIP, fw.Drop(unseen, true, &attackerHI, cp, nil),
		"sanity: attacker forging victim's source IP must be rejected when no conntrack entry exists")

	got := fw.Drop(flow, true, &attackerHI, cp, nil)
	t.Logf("attacker replaying victim's 4-tuple: Drop returned %v (nil == packet ALLOWED == spoof succeeded)", got)
	assert.Equal(t, ErrInvalidRemoteIP, got,
		"SECURITY: attacker spoofed victim's overlay source IP (192.0.2.2) by reusing an existing conntrack 4-tuple; Drop returned %v instead of rejecting", got)
```
