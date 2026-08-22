This is confirmed. The `Payload.Time` field is fully attacker-controlled — it is a `uint64` written by the peer at handshake time (`p.Time = uint64(time.Now().UnixNano())` in `handshake/machine.go`), transmitted in the handshake payload, unmarshalled without bounds checking, and copied verbatim into `result.HandshakeTime` and then `hostinfo.lastHandshakeTime`. [1](#0-0) [2](#0-1) 

### Title
Attacker-controlled handshake timestamp allows deterministic hostinfo/session takeover - (File: handshake_manager.go)

### Summary
`CheckAndComplete` uses a peer-supplied, unauthenticated 64-bit timestamp (`lastHandshakeTime`) as the sole tie-breaker deciding whether a new handshake is allowed to replace an existing, already-established tunnel for the same VPN address. Because the attacker fully controls this value and knows the comparison rule in advance (`>=` wins), they can always craft a value that guarantees victory, exactly analogous to `potCreator` knowing the future random outcome and choosing a value to guarantee winning the round.

### Finding Description
`Machine.marshalOutgoing` sets the payload's `Time` field to the local wall clock with no cryptographic binding to actual time and no server-side sanity bound: [3](#0-2) 

`processPayload` copies this attacker-supplied value directly into `Result.HandshakeTime`: [2](#0-1) 

`beginHandshake`/`continueHandshake` store it unmodified as `hostinfo.lastHandshakeTime`: [4](#0-3) [5](#0-4) 

`CheckAndComplete` then uses this attacker-controlled value as the deterministic decision rule for whether an incoming handshake is allowed to evict/replace the existing HostInfo for a peer: [6](#0-5) 

The comment on the `lastHandshakeTime` field states the field exists specifically "to avoid an attack where a handshake packet is replayed after some time" [7](#0-6)  — i.e., it is explicitly a security control, not merely diagnostic metadata. But since the value is peer-supplied and unauthenticated by any clock oracle, and the winning condition (`existingHostInfo.lastHandshakeTime >= hostinfo.lastHandshakeTime && !existingHostInfo.ConnectionState.initiator`) is fully known to any peer holding a CA-signed certificate before they craft their handshake, an attacker's own genuine peer identity can simply always set `Time` to `math.MaxUint64` (or anything higher than any prior legitimate handshake) to unconditionally win this race and force `hm.mainHostMap.unlockedAddHostInfo(hostinfo, f)` to replace/take over the existing tunnel state at will.

This mirrors the report's bug class precisely: a party who already legitimately participates in the protocol (potCreator / a certificate holder) knows in advance the exact rule that determines who "wins," and can pick a value to guarantee the win rather than being subject to unpredictable competition.

### Impact Explanation
A certificate-holding but otherwise unprivileged peer can repeatedly force itself to be "the newer handshake" for any VPN address it targets, tearing down and replacing existing hostinfo state at will (remote state poisoning of the hostmap/tunnel). Combined with `ErrExistingHostInfo` handling (which just logs and sends a `Test` message rather than rejecting the connection outright), this allows an attacker to disrupt existing, legitimate tunnels and repeatedly force renegotiation, and — because the "newer" check gates which HostInfo becomes authoritative in the mainHostMap — to insert its own handshake as the active session for a target's VPN address, subject to the underlying cert/identity still being verified. The severity is bounded by the fact that the attacker must still hold a valid CA-signed certificate for the identity being replaced (not an arbitrary spoof of a third party's identity), but within that constraint, the "newer" comparison is completely gameable rather than time-bound, defeating its own stated anti-replay purpose.

### Likelihood Explanation
High likelihood for any node possessing a valid certificate: crafting an arbitrary `Time` value requires no cryptographic breakage, only sending a handshake payload with a large `Time` field, which is a normal, honest-looking protocol field with no plausibility bound enforced anywhere in `processPayload` or `CheckAndComplete`.

### Recommendation
Do not trust peer-supplied wall-clock time as a security-relevant ordering key. Either (a) bound `Time` to a plausible window relative to the local clock and reject handshakes whose `Time` is implausibly far in the future, or (b) replace the "newer handshake" tie-break with a locally-observed, monotonic sequence (e.g., local receipt order / a locally generated nonce/counter) that cannot be forged by the remote party, so that hostinfo replacement decisions do not depend on an attacker-chosen value.

### Proof of Concept
1. Attacker (valid cert holder for VPN address `A`) already has an established tunnel with victim `V`.
2. Attacker crafts a new handshake stage-1/stage-2 payload where `Payload.Time` is set to `math.MaxUint64` (or any value greater than the currently stored `existingHostInfo.lastHandshakeTime`), via `Machine.marshalOutgoing`'s unauthenticated `p.Time` field.
3. `V` processes the packet in `beginHandshake`/`continueHandshake`, producing a `Result.HandshakeTime` equal to the attacker's chosen value [2](#0-1) .
4. `CheckAndComplete` compares `existingHostInfo.lastHandshakeTime >= hostinfo.lastHandshakeTime` — since attacker's value is always chosen to be the maximum, this is always false, so the check falls through and the attacker's new handshake replaces the existing tunnel entry [6](#0-5) .
5. Attacker can repeat this at will to force continuous session replacement/state poisoning for the target address, defeating the stated anti-replay purpose of the field.

### Citations

**File:** handshake/machine.go (L313-329)
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
```

**File:** handshake/machine.go (L382-404)
```go
func (m *Machine) marshalOutgoing(flags msgFlags) ([]byte, error) {
	if !flags.expectsPayload && !flags.expectsCert {
		return nil, nil
	}

	var p Payload
	if flags.expectsPayload {
		if !m.indexAllocated {
			index, err := m.allocIndex()
			if err != nil {
				return nil, fmt.Errorf("%w: %w", ErrIndexAllocation, err)
			}
			m.result.LocalIndex = index
			m.indexAllocated = true
		}

		if m.result.Initiator {
			p.InitiatorIndex = m.result.LocalIndex
		} else {
			p.ResponderIndex = m.result.LocalIndex
			p.InitiatorIndex = m.result.RemoteIndex
		}
		p.Time = uint64(time.Now().UnixNano())
```

**File:** handshake_manager.go (L446-452)
```go
		// Is this a newer handshake?
		if existingHostInfo.lastHandshakeTime >= hostinfo.lastHandshakeTime && !existingHostInfo.ConnectionState.initiator {
			return existingHostInfo, ErrExistingHostInfo
		}

		existingHostInfo.logger(hm.l).Info("Taking new handshake")
	}
```

**File:** handshake_manager.go (L752-758)
```go
	hostinfo := &HostInfo{
		ConnectionState:   newConnectionStateFromResult(result),
		localIndexId:      result.LocalIndex,
		remoteIndexId:     result.RemoteIndex,
		vpnAddrs:          vpnAddrs,
		HandshakePacket:   make(map[uint8][]byte, 0),
		lastHandshakeTime: result.HandshakeTime,
```

**File:** handshake_manager.go (L882-883)
```go
	hostinfo.remoteIndexId = result.RemoteIndex
	hostinfo.lastHandshakeTime = result.HandshakeTime
```

**File:** hostmap.go (L270-273)
```go
	// lastHandshakeTime records the time the remote side told us about at the stage when the handshake was completed locally
	// Stage 1 packet will contain it if I am a responder, stage 2 packet if I am an initiator
	// This is used to avoid an attack where a handshake packet is replayed after some time
	lastHandshakeTime uint64
```
