### Title
Handshake Responder Redoes Expensive Noise/Cert Crypto on Every Replayed Stage-1 Packet Before Deduplication - ([File: handshake_manager.go])

### Summary
`HandshakeManager.beginHandshake` unconditionally builds a fresh `handshake.Machine` and runs full asymmetric Noise/cert-verification cryptography for every incoming stage-1 packet, before any duplicate/replay check is performed. An attacker who has merely observed (not forged) one legitimate, already-used stage-1 handshake packet from a real peer can resend that exact captured packet in a tight loop, forcing the responder to redo the expensive DH/signature work on each replay, with deduplication only happening afterward in `CheckAndComplete`. This mirrors the report's bug class: knowledge of previously valid signed data, reusable within its validity window, being exploited repeatedly through a scripted loop to disproportionately cost the defender.

### Finding Description
`HandshakeManager.HandleIncoming` routes every packet with `h.MessageCounter == 1` (a stage-1 message) directly to `beginHandshake`, gated only on `RemoteIndex == 0` and the remote allow-list check: [1](#0-0) 

`beginHandshake` then always allocates a new `handshake.Machine` and calls `machine.ProcessPacket`, which performs the full Noise `ReadMessage` (X25519/P256 DH) and certificate signature/CA-chain verification (`certVerifier`) — *before* checking whether this exact packet has already been handled: [2](#0-1) 

Only after this expensive cryptographic work completes, and a full `HostInfo` is constructed, does the code call `CheckAndComplete`, which is where an exact-byte match against a previously stored handshake packet is finally detected and rejected as `ErrAlreadySeen`: [3](#0-2) 

Because the cheap `bytes.Equal` dedup check happens only at the very end of an expensive path, an attacker who simply captures a single legitimate stage-1 packet (they need no CA-signed certificate of their own — they are replaying someone else's already-valid packet) can resend it in a loop to force the responder to repeatedly pay the full asymmetric-crypto cost, analogous to the oracle-price report's attacker repeatedly reusing two already-known, still-valid signed values to profit at the defender's disproportionate expense.

### Impact Explanation
Each replayed stage-1 packet forces a real ECDH computation and certificate signature verification on the responder, work that is orders of magnitude more expensive than the eventual duplicate check that discards it. A remote, unauthenticated attacker (no valid CA-issued certificate required) can amplify a single captured packet into sustained CPU load via a simple send loop, degrading or denying handshake processing for legitimate peers — a remote resource-exhaustion condition reachable purely through replay of previously-observed valid handshake data.

### Likelihood Explanation
Likelihood is high for any attacker positioned to observe UDP traffic to a Nebula listener (or who is themselves a legitimate, already-torn-down peer). Capturing one stage-1 packet requires no cryptographic material of the attacker's own; nebula's on-wire handshake packets are otherwise unauthenticated at the UDP layer until Noise/cert processing completes, so nothing prevents a scripted resend loop.

### Recommendation
Perform a cheap, pre-crypto duplicate/replay check for stage-1 packets (e.g., match against the exact bytes of any pending/completed handshake keyed by source/packet hash) before constructing a `handshake.Machine` and invoking `ProcessPacket`. Rate-limit or cache verification results per source address/packet fingerprint so identical replayed stage-1 packets are rejected without repeating the DH and signature-verification work.

### Proof of Concept
1. Legitimate peer A completes a handshake with responder B; attacker M passively captures A's stage-1 UDP packet.
2. M repeatedly re-injects the exact captured stage-1 packet to B (`RemoteIndex == 0`, `MessageCounter == 1`).
3. Each injection reaches `HandshakeManager.beginHandshake`, which creates a new `handshake.Machine` and runs `ProcessPacket` (full ECDH + cert chain verification) before `CheckAndComplete` finally detects the byte-identical duplicate and discards the work.
4. Looping step 2 at high rate drives sustained CPU consumption on B proportional to the number of replays, independent of any certificate M itself holds.

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

**File:** handshake_manager.go (L430-444)
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
```

**File:** handshake_manager.go (L712-726)
```go
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
```
