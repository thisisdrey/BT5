Found a valid analog: an unbounded, peer-controlled value (`handshake.Payload.Time`) is accepted with no sanity/plausibility checking and is then used as the sole tie-breaker for whether a new handshake is allowed to replace an existing, live tunnel — the same "trust the external value regardless of its actual value" pattern flagged in the PriceOracle report.

### Title
Handshake replacement accepts an unvalidated, attacker-controlled `Time` value with no bounds/freshness check - ([File: handshake_manager.go])

### Summary
`CheckAndComplete` decides whether an incoming handshake is allowed to displace the existing tunnel for a `vpnAddr` purely by comparing `hostinfo.lastHandshakeTime` against the value already stored for that peer [1](#0-0) . That value originates directly from the wire field `Payload.Time`, a `uint64` supplied by the remote peer, decoded with no range, monotonicity, or clock-skew check at all — any value 0..2^64-1 is accepted [2](#0-1) . `unmarshalPayloadDetails` only validates the protobuf wire type and varint decoding, never the value's plausibility [2](#0-1) .

### Finding Description
On the initiator side, `Time` is populated from the wall clock at message-build time and forwarded unmodified: `p.Time = uint64(time.Now().UnixNano())` [3](#0-2) . On the receiving side, `processPayload` copies the field straight into `result.HandshakeTime` with no bounds check beyond "is the field present" [4](#0-3) . That result is copied into `hostinfo.lastHandshakeTime` on both the responder path (`beginHandshake`) and the initiator path (`continueHandshake`) [5](#0-4) [6](#0-5) .

This field is trusted for a security-relevant decision: `CheckAndComplete` uses it to gate whether an already-authenticated peer's *new* handshake attempt can supersede the currently-established tunnel:
```go
if existingHostInfo.lastHandshakeTime >= hostinfo.lastHandshakeTime && !existingHostInfo.ConnectionState.initiator {
    return existingHostInfo, ErrExistingHostInfo
}
``` [1](#0-0) 

Exactly like the `PriceOracle` code that used whatever value Chainlink returned "regardless of its actual value," this comparison uses whatever `Time` a peer chose to send, with no upper/lower bound, no comparison against local wall-clock, and no historical-consistency check. Since a certificate holder fully controls the plaintext content that becomes `Payload.Time` before it is authenticated by the Noise handshake, they can freely set it to `math.MaxUint64` (or any arbitrarily large value) on a legitimate future handshake. Once accepted, `lastHandshakeTime` is poisoned to a huge value, and every subsequent legitimate re-handshake attempt from that peer's real client — whose `Time` will be a normal, much smaller `time.Now().UnixNano()` — will be rejected via the `>=` comparison in `CheckAndComplete`, because `existingHostInfo.lastHandshakeTime >= hostinfo.lastHandshakeTime` holds by construction until real wall-clock time catches up (effectively forever, since it's nanoseconds since epoch). This can deny legitimate tunnel re-establishment/roaming for that peer.

### Impact Explanation
This is a remote state-poisoning issue: a certificate-holder can, via a value fully under their control and never range-checked, cause future genuine handshake attempts (including their own subsequent legitimate ones, or a race condition triggered by another party) to be treated as "too old" and rejected (`ErrExistingHostInfo` in `handleCheckAndCompleteError`, which then just fires a `TestRequest` rather than replacing the tunnel) [7](#0-6) . This denies rekeying/re-handshake for the lifetime of that hostinfo entry, a persistent DoS against tunnel re-establishment that stems directly from the missing outlier/bounds check on externally supplied data — the same bug class as the reported oracle issue (trusting an external, attacker-influenced numeric value with no sanity bound before using it in a security decision).

### Likelihood Explanation
Reachable by any already-provisioned certificate holder without needing anything beyond a normal handshake — no lighthouse or CA compromise required, and no malicious-peer/host-access exclusion applies since this is standard handshake-authentication logic (`handshake/payload.go`, `handshake_manager.go`) reachable during ordinary tunnel establishment.

### Recommendation
Validate `Payload.Time` against a reasonable bound (e.g., reject values wildly in the future/past relative to local wall clock, or use a monotonic per-tunnel sequence number instead of peer-supplied wall-clock time) before using it as an ordering/tie-break signal in `CheckAndComplete`. At minimum, clamp accepted `Time` to `[now-δ, now+δ]` for a small δ so a single malicious value can't permanently poison `lastHandshakeTime`.

### Proof of Concept
1. Peer A performs a normal handshake with Peer B; `lastHandshakeTime` is set from `Payload.Time = time.Now().UnixNano()` on both sides.
2. Peer A (still a legitimately certificate-holding, but compromised/misbehaving client) crafts a subsequent handshake stage-0/stage-1 message with `Payload.Time = math.MaxUint64` instead of the real timestamp — `MarshalPayload`/`UnmarshalPayload` place no constraint on the value [2](#0-1) .
3. Peer B accepts this handshake (assuming it authenticates normally), and `hostinfo.lastHandshakeTime` is set to `math.MaxUint64` [6](#0-5) .
4. Any subsequent, entirely legitimate handshake attempt from A (with a real, small `time.Now().UnixNano()` value) is now permanently treated as "too old" by `CheckAndComplete`'s `>=` comparison [1](#0-0) , and Peer B refuses to replace the existing tunnel, instead only sending a `TestRequest` [7](#0-6) .

### Citations

**File:** handshake_manager.go (L446-449)
```go
		// Is this a newer handshake?
		if existingHostInfo.lastHandshakeTime >= hostinfo.lastHandshakeTime && !existingHostInfo.ConnectionState.initiator {
			return existingHostInfo, ErrExistingHostInfo
		}
```

**File:** handshake_manager.go (L757-758)
```go
		HandshakePacket:   make(map[uint8][]byte, 0),
		lastHandshakeTime: result.HandshakeTime,
```

**File:** handshake_manager.go (L882-883)
```go
	hostinfo.remoteIndexId = result.RemoteIndex
	hostinfo.lastHandshakeTime = result.HandshakeTime
```

**File:** handshake_manager.go (L1115-1129)
```go
	case ErrExistingHostInfo:
		f.l.Info("Handshake too old",
			"vpnAddrs", hostinfo.vpnAddrs,
			"from", via,
			"certName", peerCert.Certificate.Name(),
			"certVersion", peerCert.Certificate.Version(),
			"fingerprint", peerCert.Fingerprint,
			"issuer", peerCert.Certificate.Issuer(),
			"oldHandshakeTime", existing.lastHandshakeTime,
			"newHandshakeTime", hostinfo.lastHandshakeTime,
			"initiatorIndex", hostinfo.remoteIndexId,
			"responderIndex", hostinfo.localIndexId,
			"handshake", hsFields,
		)
		f.SendMessageToVpnAddr(header.Test, header.TestRequest, hostinfo.vpnAddrs[0], []byte(""), make([]byte, 12, 12), make([]byte, mtu))
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

**File:** handshake/machine.go (L326-329)
```go
		}
		m.result.RemoteIndex = remoteIndex
		m.result.HandshakeTime = payload.Time
		m.payloadSet = true
```

**File:** handshake/machine.go (L404-404)
```go
		p.Time = uint64(time.Now().UnixNano())
```
