## Title
Handshake stage-0 messages carry a `Time` field but have no enforced freshness deadline, allowing delayed replay of a captured handshake to poison host/remote state - (`File: handshake_manager.go`)

### Summary
The external report's root bug class is that a signed/authorized action (a Uniswap swap call) carries no expiration deadline, so it can be captured and re-submitted later by a third party when conditions are more favorable to them. The reachable analog in nebula is the handshake initiation message (`msg1`/stage-0): it embeds a `Time` field in `handshake.Payload` [1](#0-0)  but this value is never checked against the current wall-clock time or any maximum age window anywhere in the handshake state machine. The only freshness guard that exists is a relative comparison against an already-established tunnel for the same VPN address, not an absolute deadline.

### Finding Description
`handshake.Machine.processPayload` extracts `payload.Time` into `Result.HandshakeTime` but performs no bound-checking against the current time; it only verifies the field is non-zero-consistent with the expected payload shape: [2](#0-1) 

The value is later stored as `lastHandshakeTime` on the `HostInfo`, explicitly documented as an anti-replay mechanism: [3](#0-2) 

However, `HandshakeManager.CheckAndComplete` only rejects a new handshake as "too old" when a **pre-existing** `HostInfo` for that VPN address already has a `lastHandshakeTime` greater than or equal to the incoming one: [4](#0-3) 

When no such prior `HostInfo` exists yet for the peer, there is nothing preventing a stage-0 packet captured off the wire at any point in the past from being replayed and accepted, as long as the embedded certificate has not expired. `beginHandshake` processes the packet, validates the certificate (`validatePeerCert`), and unconditionally calls `hostinfo.SetRemote(via.UdpAddr)`, binding the peer's VPN address to whatever UDP source address delivered the replayed packet: [5](#0-4) 

This is directly demonstrated by the project's own e2e test, where a msg1 captured before a handshake timeout is delivered to the responder well after the original attempt has expired, and the responder still completes a handshake and creates a tunnel from it: [6](#0-5) 

There is no analog of a `deadline` parameter anywhere in the handshake proto/wire format either - the `Time` field is informational only, and the only anti-DoS field (`Cookie`) is explicitly noted as never implemented: [7](#0-6) 

### Impact Explanation
An on-path attacker who captures a legitimate stage-0 handshake packet can hold it and replay it from an arbitrary source address at a time of their choosing (bounded only by the embedded certificate's `NotAfter`). Because `beginHandshake` calls `hostinfo.SetRemote(via.UdpAddr)` unconditionally on first-seen handshakes for a VPN address, this can cause the responder to bind/re-bind a peer's VPN address to an attacker-controlled or otherwise stale UDP endpoint, i.e., remote-state poisoning of the hostmap/lighthouse-learned address for that peer, and can force creation of new tunnel/hostinfo state from stale key material outside the control of either legitimate endpoint. This mirrors the "hold the transaction until advantageous" MEV pattern from the source report, translated to network state.

### Likelihood Explanation
Exploitation requires only the ability to observe/capture one legitimate handshake packet and resend it later - it does not require possession of a valid CA-signed certificate, a compromised peer, or lighthouse trust. The only constraint is the captured certificate's expiration window, which for reasonably long-lived host certs (weeks/months, as is typical for nebula deployments) leaves an ample window for delayed replay.

### Recommendation
Enforce an absolute freshness deadline on the handshake `Time` field independent of any pre-existing `HostInfo` state - e.g., reject any stage-0 message whose `Time` differs from the local clock by more than a small bounded window (analogous to adding a user/protocol-level deadline to the AMM swap). This closes the gap for first-contact handshakes where the relative `lastHandshakeTime` comparison in `CheckAndComplete` provides no protection.

### Proof of Concept
1. Attacker passively captures a valid stage-0 handshake packet (`msg1`) sent from host A to host B before any tunnel between them has completed (as in `TestHandshakeLateResponse`) [8](#0-7) .
2. Original handshake attempt on A's side times out and is purged (`hm.DeleteHostInfo(hostinfo)` in `handleOutbound`) [9](#0-8) .
3. Attacker replays the captured `msg1` to B from an address of their choosing at a later, chosen time.
4. B has no existing `HostInfo` for A's VPN address, so `CheckAndComplete`'s relative freshness check never triggers [4](#0-3) , and B completes the handshake, binding A's VPN address to the replaying source via `SetRemote` [10](#0-9) .

### Citations

**File:** handshake/payload.go (L15-23)
```go
// Payload represents the decoded fields of a handshake message.
// Wire format is protobuf-compatible with NebulaHandshake{Details: NebulaHandshakeDetails{...}}.
type Payload struct {
	Cert           []byte
	InitiatorIndex uint32
	ResponderIndex uint32
	Time           uint64
	CertVersion    uint32
}
```

**File:** handshake/machine.go (L313-330)
```go
	// Process payload
	if flags.expectsPayload {
		var remoteIndex uint32
		if m.result.Initiator {
			remoteIndex = payload.ResponderIndex
		} else {
			remoteIndex = payload.InitiatorIndex
		}
		// The payload presence check above can be satisfied by Time alone, so a payload
		// could still carry a zero index here. We need to reject it.
		if remoteIndex == 0 {
			m.failed = true
			return ErrInvalidRemoteIndex
		}
		m.result.RemoteIndex = remoteIndex
		m.result.HandshakeTime = payload.Time
		m.payloadSet = true
	}
```

**File:** hostmap.go (L270-273)
```go
	// lastHandshakeTime records the time the remote side told us about at the stage when the handshake was completed locally
	// Stage 1 packet will contain it if I am a responder, stage 2 packet if I am an initiator
	// This is used to avoid an attack where a handshake packet is replayed after some time
	lastHandshakeTime uint64
```

**File:** handshake_manager.go (L216-236)
```go
	// If we are out of time, clean up
	if hh.counter >= hm.config.retries {
		fields := []any{
			"udpAddrs", hh.hostinfo.remotes.CopyAddrs(hm.mainHostMap.GetPreferredRanges()),
			"initiatorIndex", hh.hostinfo.localIndexId,
			"durationNs", time.Since(hh.startTime).Nanoseconds(),
		}
		// hh.machine can be nil here if buildStage0Packet never succeeded
		// (e.g., no certificate available). In that case there's no useful
		// handshake metadata to log.
		if hh.machine != nil {
			fields = append(fields, "handshake", m{
				"stage": uint64(hh.machine.MessageIndex()),
				"style": header.SubTypeName(header.Handshake, hh.machine.Subtype()),
			})
		}
		hh.hostinfo.logger(hm.l).Info("Handshake timed out", fields...)
		hm.metricTimedOut.Inc(1)
		hm.DeleteHostInfo(hostinfo)
		return
	}
```

**File:** handshake_manager.go (L436-452)
```go
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
```

**File:** handshake_manager.go (L746-795)
```go
	// Validate peer identity
	vpnAddrs, anyVpnAddrsInCommon, ok := hm.validatePeerCert(via, remoteCert)
	if !ok {
		return
	}

	hostinfo := &HostInfo{
		ConnectionState:   newConnectionStateFromResult(result),
		localIndexId:      result.LocalIndex,
		remoteIndexId:     result.RemoteIndex,
		vpnAddrs:          vpnAddrs,
		HandshakePacket:   make(map[uint8][]byte, 0),
		lastHandshakeTime: result.HandshakeTime,
		relayState: RelayState{
			relays:         nil,
			relayForByAddr: map[netip.Addr]*Relay{},
			relayForByIdx:  map[uint32]*Relay{},
		},
	}

	msg := "Handshake message received"
	if !anyVpnAddrsInCommon {
		msg = "Handshake message received, but no vpnNetworks in common."
	}
	f.l.Info(msg,
		"vpnAddrs", vpnAddrs,
		"from", via,
		"certName", remoteCert.Certificate.Name(),
		"certVersion", remoteCert.Certificate.Version(),
		"fingerprint", remoteCert.Fingerprint,
		"issuer", remoteCert.Certificate.Issuer(),
		"initiatorIndex", result.RemoteIndex,
		"responderIndex", result.LocalIndex,
		"handshake", m{"stage": uint64(machine.MessageIndex()), "style": header.SubTypeName(header.Handshake, machine.Subtype())},
	)

	// packet aliases the listener's incoming buffer, so this copy must stay.
	hostinfo.HandshakePacket[handshakePacketStage0] = make([]byte, len(packet[header.Len:]))
	copy(hostinfo.HandshakePacket[handshakePacketStage0], packet[header.Len:])

	// response was freshly allocated by ProcessPacket; safe to retain directly.
	if response != nil {
		hostinfo.HandshakePacket[handshakePacketStage2] = response
	}

	hostinfo.remotes = f.lightHouse.QueryCache(vpnAddrs)
	if !via.IsRelayed {
		hostinfo.SetRemote(via.UdpAddr)
	}
	hostinfo.buildNetworks(f.myVpnNetworksTable, remoteCert.Certificate)
```

**File:** e2e/handshake_manager_test.go (L231-268)
```go
func TestHandshakeLateResponse(t *testing.T) {
	t.Parallel()
	// After a handshake times out, a late response should be silently ignored
	// with no new tunnels created.

	ca, _, caKey, _ := cert_test.NewTestCaCert(cert.Version1, cert.Curve_CURVE25519, time.Now(), time.Now().Add(10*time.Minute), nil, nil, []string{})
	myControl, myVpnIpNet, _, _ := newSimpleServer(cert.Version1, ca, caKey, "me", "10.128.0.1/24", m{
		"handshakes": m{
			"try_interval": "200ms",
			"retries":      2,
		},
	})
	theirControl, theirVpnIpNet, theirUdpAddr, _ := newSimpleServer(cert.Version1, ca, caKey, "them", "10.128.0.2/24", nil)

	myControl.InjectLightHouseAddr(theirVpnIpNet[0].Addr(), theirUdpAddr)

	myControl.Start()
	theirControl.Start()

	t.Log("Trigger handshake from me")
	myControl.InjectTunPacket(BuildTunUDPPacket(theirVpnIpNet[0].Addr(), 80, myVpnIpNet[0].Addr(), 80, []byte("Hi")))

	t.Log("Grab msg1 but don't deliver")
	msg1 := myControl.GetFromUDP(true)

	t.Log("Wait for handshake to time out")
	for i := 0; i < 5; i++ {
		time.Sleep(300 * time.Millisecond)
		myControl.GetFromUDP(false)
	}

	t.Log("Confirm no pending handshakes remain")
	assert.Empty(t, myControl.ListHostmapHosts(true))

	t.Log("Deliver old msg1 to them, they create a tunnel")
	theirControl.InjectUDPPacket(msg1)
	resp := theirControl.GetFromUDP(true)
	assert.NotNil(t, resp)
```

**File:** handshake/handshake.proto (L17-29)
```text
message NebulaHandshakeDetails {
  bytes Cert = 1;
  uint32 InitiatorIndex = 2;
  uint32 ResponderIndex = 3;
  // Cookie was reserved for an anti-DoS mechanism that was never
  // implemented. No released version of nebula has ever populated it; the
  // hand-written parser silently skips it on read.
  uint64 Cookie = 4 [deprecated = true];
  uint64 Time = 5;
  uint32 CertVersion = 8;
  // reserved for WIP multiport
  reserved 6, 7;
}
```
