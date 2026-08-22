### Title
Missing deadline/freshness check on captured handshake initiation packets enables unauthenticated replay and hostmap/resource-state poisoning - (File: `handshake/machine.go`, `handshake_manager.go`)

### Summary
Nebula's Noise-based handshake payload carries a `Time` field (`payload.Time`) that is parsed and stored as `Result.HandshakeTime` / `HostInfo.lastHandshakeTime`, but this value is never checked against the current wall-clock time anywhere in the responder path. A stage-1 handshake packet (`msg1`) captured by an on-path or off-path attacker who has no CA-signed certificate of their own can be replayed at an arbitrary point in the future; the responder will still fully process it (asymmetric Noise DH, certificate reconstruction/verification, index allocation, and `HostInfo`/hostmap insertion) exactly as if it were fresh. This mirrors the AfEth report's root cause: a time-sensitive action (`deposit`/`withdraw`, here a handshake attempt) lacks an expiration/deadline check, so a captured, stale action can be maliciously replayed later and be treated as valid.

### Finding Description
The handshake payload's `Time` field is populated by the sender in `marshalOutgoing`: [1](#0-0) 

and is consumed on the receiving side purely to populate `Result.HandshakeTime`/`hostinfo.lastHandshakeTime`, with no comparison to `time.Now()`: [2](#0-1) 

`beginHandshake` (the entry point for a brand-new, *unauthenticated* stage-1 packet from any UDP source) runs the full noise `ReadMessage`, certificate recombination and CA-pool verification, and then unconditionally builds a new `HostInfo` using this unchecked, attacker-replayable timestamp: [3](#0-2) 

The only place `lastHandshakeTime` is later used is as an *ordering* tiebreaker in `CheckAndComplete`, not as a freshness/expiry check: [4](#0-3) 

Contrast this with the responder-side lack of validation to the *initiator's* local timeout logic, which only bounds how long the initiator's own local pending state lives, not how long a captured packet remains "valid" if replayed to the peer: [5](#0-4) 

The e2e test `TestHandshakeLateResponse` documents exactly this asymmetry: once the initiator's local timeout fires, it discards local pending state, but a delayed `msg1` delivered afterward to the responder still succeeds in creating a tunnel — proving there is no deadline enforced on the wire-level handshake initiation itself: [6](#0-5) 

Certificate expiry (`Expired(t)`) is checked, but that only bounds the *certificate's* validity window (which can be hours/days), not the handshake attempt's freshness — an attacker can replay a captured `msg1` at any point within the certificate's lifetime, not just within the intended handshake retry window.

### Impact Explanation
An attacker with no CA-signed certificate — merely someone who has observed a single legitimate handshake initiation packet on the wire — can:
1. Replay that captured `msg1` to the responder at any later time (bounded only by the signer's/cert's expiry, not by any handshake-specific deadline).
2. Force the responder to repeatedly perform expensive asymmetric Noise/ECDH operations, certificate reconstruction, and CA-pool verification, and to allocate a new local index and `HostInfo` entry (or resend a stale cached stage-2 response via the `ErrAlreadySeen` path) for a handshake attempt the legitimate peer may have already abandoned.
3. Poison remote hostmap/pending-handshake state with stale, attacker-controlled-timing entries repeatedly, since nothing invalidates the packet based on its age.

This satisfies "remote state poisoning" and CPU/resource-exhaustion impact categories: an unauthenticated party can trigger unbounded repeated cryptographic work and hostmap churn using a single captured packet, at a time of their choosing, with no deadline gate to stop it.

### Likelihood Explanation
Likelihood is moderate-to-high in adversarial network conditions: any attacker capable of observing UDP traffic between two Nebula nodes (a common assumption for a VPN mesh operating over untrusted networks/relays) can capture one `msg1` and replay it indefinitely. No possession of a valid private key or CA-signed certificate is required to perform the replay — only capture-and-resend of ciphertext bytes.

### Recommendation
Add an explicit deadline/freshness check on the handshake payload's `Time` field in `Machine.processPayload` (or immediately in `beginHandshake`/`continueHandshake`), rejecting stage-1 (and subsequent) handshake messages whose `payload.Time` is older than a small, configurable skew window (e.g., a few seconds, similar to the existing `handshakes.try_interval`/`retries` timeout budget). This closes the gap between the initiator's local handshake timeout and the responder's unconditional acceptance of arbitrarily old, replayed handshake-initiation packets.

### Proof of Concept
1. Attacker passively captures a legitimate `msg1` (stage-1 handshake packet, type `header.Handshake`, subtype `header.HandshakeIXPSK0`) sent from host A to host B, as demonstrated by the packet construction/injection pattern in [7](#0-6) .
2. Attacker waits an arbitrary amount of time (long after A's own handshake attempt has timed out per `hsTimeout`/`retries`, as shown by [8](#0-7) ).
3. Attacker (or a MITM relay) re-injects the exact same `msg1` bytes toward B's UDP listener from any source address.
4. B's `HandshakeManager.HandleIncoming` → `beginHandshake` processes it with no freshness check, performs the full Noise/cert verification, and creates a new `HostInfo`/pending-handshake state (or resends a cached stage-2 via `ErrAlreadySeen`), exactly as `TestHandshakeLateResponse` confirms happens for a late-delivered `msg1` [9](#0-8) .

### Citations

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

**File:** handshake/machine.go (L398-405)
```go
		if m.result.Initiator {
			p.InitiatorIndex = m.result.LocalIndex
		} else {
			p.ResponderIndex = m.result.LocalIndex
			p.InitiatorIndex = m.result.RemoteIndex
		}
		p.Time = uint64(time.Now().UnixNano())
	}
```

**File:** handshake_manager.go (L207-246)
```go
func (hm *HandshakeManager) handleOutbound(vpnIp netip.Addr, lighthouseTriggered bool) {
	hh := hm.queryVpnIp(vpnIp)
	if hh == nil {
		return
	}
	hh.Lock()
	defer hh.Unlock()

	hostinfo := hh.hostinfo
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

	// Increment the counter to increase our delay, linear backoff
	hh.counter++

	// Check if we have a handshake packet to transmit yet
	if !hh.ready {
		if !hm.buildStage0Packet(hh) {
			hm.OutboundHandshakeTimer.Add(vpnIp, hm.config.tryInterval*time.Duration(hh.counter))
			return
		}
```

**File:** handshake_manager.go (L430-452)
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
```

**File:** handshake_manager.go (L701-764)
```go
func (hm *HandshakeManager) beginHandshake(via ViaSender, packet []byte, h *header.H) {
	f := hm.f
	cs := f.pki.getCertState()

	v := cs.DefaultVersion()
	if cs.GetCredential(v) == nil {
		f.l.Error("Unable to handshake with host because no certificate is available",
			"from", via, "certVersion", v)
		return
	}

	machine, err := handshake.NewMachine(
		v, cs.GetCredential,
		hm.certVerifier(), func() (uint32, error) { return generateIndex(f.l) },
		false, header.HandshakeIXPSK0,
	)
	if err != nil {
		f.l.Error("Failed to create handshake machine", "from", via, "error", err)
		return
	}

	response, result, err := machine.ProcessPacket(nil, packet)
	if err != nil {
		f.l.Error("Failed to process handshake packet", "from", via, "error", err)
		return
	}

	if result == nil {
		// Multi-message pattern: the responder Machine would need to be
		// registered in hm.indexes so a future inbound packet finds it via
		// continueHandshake. The current manager doesn't do that yet, so
		// fail loudly rather than silently dropping the in-flight handshake.
		// TODO: support multi-message responder flows (XX, pqIX, etc.).
		// See also the IX-shaped cipher key assignment in handshake.Machine.
		f.l.Error("multi-message handshake responder is not supported",
			"from", via, "error", handshake.ErrMultiMessageUnsupported)
		return
	}

	remoteCert := result.RemoteCert
	if remoteCert == nil {
		f.l.Error("Handshake did not produce a peer certificate", "from", via)
		return
	}

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
```

**File:** e2e/handshake_manager_test.go (L20-28)
```go
// makeHandshakePacket creates a handshake packet with the given parameters.
func makeHandshakePacket(from, to netip.AddrPort, subtype header.MessageSubType, remoteIndex uint32, counter uint64) *udp.Packet {
	data := make([]byte, 200)
	header.Encode(data, header.Version, header.Handshake, subtype, remoteIndex, counter)
	for i := header.Len; i < len(data); i++ {
		data[i] = byte(i)
	}
	return &udp.Packet{To: to, From: from, Data: data}
}
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
