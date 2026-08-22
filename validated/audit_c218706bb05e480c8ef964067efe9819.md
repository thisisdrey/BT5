### Title
`HandshakeTime` timestamp is never validated against wall-clock time before being used to gate handshake replacement - ([File: handshake_manager.go])

### Summary
Nebula's handshake payload carries a peer-supplied `Time` field (`HandshakeTime`) that is intended to prevent replay of stale handshake packets. This value is never checked against the actual current time — it is only compared against a previously cached value, and even that comparison is skipped entirely on one code path. This mirrors the Buffer Finance H-05 bug class: a timestamp embedded in an otherwise-authenticated message is never cross-validated against an authoritative/real clock, so a captured message can be replayed later to manipulate state.

### Finding Description
The handshake wire payload defines a `Time` field [1](#0-0)  that is decoded verbatim from attacker-controlled bytes with no bounds or freshness check [2](#0-1) .

`Machine.processPayload` copies this raw value directly into `Result.HandshakeTime` with no comparison to `time.Now()` or any other authoritative clock: [3](#0-2) 

`HandshakeManager` stores this unauthenticated value as `hostinfo.lastHandshakeTime`, and the field's own doc comment states its entire purpose is anti-replay: "This is used to avoid an attack where a handshake packet is replayed after some time" [4](#0-3) .

The only place this value is actually checked is in `CheckAndComplete`: [5](#0-4) 

The check `existingHostInfo.lastHandshakeTime >= hostinfo.lastHandshakeTime && !existingHostInfo.ConnectionState.initiator` has two problems:
1. It never validates `hostinfo.lastHandshakeTime` (the new, peer-supplied value) against real wall-clock time — it only compares it to a previously cached peer-supplied value, so an attacker who can replay any previously valid handshake byte stream controls both sides of the comparison.
2. When the existing hostinfo was created because *we* are the initiator of the current tunnel (`existingHostInfo.ConnectionState.initiator == true`), the ordering check is skipped entirely and the code unconditionally logs "Taking new handshake" and replaces the tracked tunnel state, regardless of whether the incoming handshake's `HandshakeTime` is stale.

Because a stage-1 handshake packet is just raw UDP bytes tied to a certificate that was valid when originally sent, an on-path/off-path network attacker (with no CA-signed certificate of their own) can capture a legitimate initiator's handshake packet at any point and replay it later. If the local side currently believes itself to be the initiator of an established tunnel to that peer, the replayed, stale handshake bypasses the "is this newer" guard completely and is accepted, replacing the live hostinfo/tunnel state.

### Impact Explanation
This allows a network attacker with no valid certificate to poison/replace an active tunnel's hostinfo state using a previously captured, now-stale handshake, because the anti-replay timestamp field (`HandshakeTime`) is never validated against real time and the staleness comparison itself is bypassed for the initiator-side branch. This falls under remote state poisoning of the hostmap/handshake state, matching the "remote state poisoning" impact category.

### Likelihood Explanation
Exploitation requires only the ability to observe/capture one legitimate handshake packet on the wire (no cryptographic material, no certificate) and replay it later while the target still holds an initiator-side hostinfo for that peer — a realistic network position for an on-path attacker, consistent with nebula's existing UDP threat model.

### Recommendation
- Validate `HandshakeTime` against the local wall-clock (`time.Now()`) with a bounded skew window, rejecting handshake payloads whose `Time` is too old or too far in the future, similar to certificate `NotBefore`/`NotAfter` validation in `cert/ca_pool.go`.
- Remove the initiator-side exception in `CheckAndComplete` (`!existingHostInfo.ConnectionState.initiator`) or otherwise ensure the staleness check applies uniformly regardless of which side is the local initiator, so a replayed/older handshake packet can never unconditionally replace a live tunnel.

### Proof of Concept
1. Attacker passively captures a legitimate stage-1 handshake UDP packet sent from host A to host B while A is the initiator of an active tunnel (`existingHostInfo.ConnectionState.initiator == true` on A's side for a prior successful handshake).
2. At a later time, attacker replays that captured stage-1 packet to A (or triggers B to reprocess a corresponding stage-2 aimed at A), causing `beginHandshake`/`continueHandshake` → `CheckAndComplete` to run on A with the old `HandshakeTime`.
3. Because A's existing hostinfo has `ConnectionState.initiator == true`, the `!existingHostInfo.ConnectionState.initiator` guard is false, so the `lastHandshakeTime` ordering check in `handshake_manager.go` lines 447-449 is skipped entirely, and "Taking new handshake" unconditionally replaces A's tracked hostinfo with the stale replayed handshake state — without any check that `HandshakeTime` reflects the actual current time.

### Citations

**File:** handshake/handshake.proto (L17-25)
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
```

**File:** handshake/payload.go (L144-153)
```go
		case fieldTime:
			if typ != protowire.VarintType {
				return errInvalidHandshakeDetails
			}
			v, n := protowire.ConsumeVarint(b)
			if n < 0 {
				return errInvalidHandshakeDetails
			}
			p.Time = v
			b = b[n:]
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
