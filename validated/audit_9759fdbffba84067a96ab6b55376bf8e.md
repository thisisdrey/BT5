### Title
Unauthenticated `RecvError` packet allows any attacker to force teardown of an established tunnel - (File: outside.go)

### Summary
The reported Treasury bug is a class of vulnerability where an unauthenticated actor can send a message that mutates critical shared protocol state (inflating `_totalValue`), causing legitimate operations to fail/DoS. The analogous class in Nebula is a control-plane message that is processed and acted upon **before any certificate-based authentication or Noise handshake completes**, allowing an attacker with no CA-signed certificate to mutate tunnel state and disrupt legitimate connectivity. This is present in the `header.RecvError` handling path.

### Finding Description
`readOutsidePackets` dispatches `header.RecvError` packets directly to `f.handleRecvError` before any decryption or peer-certificate verification — it is one of only two message types processed in the "Unencrypted packets" branch (the other being `Handshake`): [1](#0-0) 

`handleRecvError` looks up the tunnel by the cleartext `RemoteIndex` field carried in the wire header, and — if the sender's UDP source address either matches the tunnel's currently known remote or the remote is not yet set (`!hr.IsValid()`) — unconditionally tears the tunnel down and deletes the pending handshake state: [2](#0-1) 

The `RemoteIndex` field is transmitted in cleartext on every packet (it is not part of the encrypted payload): [3](#0-2) [4](#0-3) 

No Noise handshake, no certificate check, and no HMAC/AEAD authentication of the `RecvError` packet itself is performed — the only "check" is a source-address comparison, which is defeatable via source-IP/port spoofing over UDP, or trivially satisfied when the target's remote address hasn't been learned/roamed yet. This mirrors the Treasury bug's core defect: an operation that mutates authoritative shared state (`_totalValue` in Treasury; the hostmap/tunnel state in Nebula) is reachable by an unauthenticated party, with no whitelist/authentication gate before the state-mutating action occurs.

The project's own history confirms this class of concern was already known: it added an `accept_recv_error`/`send_recv_error` config knob because unauthenticated `recv_error` packets could disclose or affect host state, and specifically restricted sending of these messages "when a packet is received outside the allowable counter window" in 1.9.7: [5](#0-4) [6](#0-5) 

However, on the **receive/accept** side, `handleRecvError` still performs no cryptographic authentication of the RecvError packet before acting on it — it only gates on a config toggle (`acceptRecvErrorConfig.ShouldRecvError`) and a spoofable source-address match.

### Impact Explanation
An attacker who can spoof UDP source packets (or who is on-path and can observe the cleartext `RemoteIndex`, e.g. an on-link/NAT observer) can:
- Force `f.closeTunnel(hostinfo)` on an arbitrary active tunnel, and also purge the pending handshake state via `f.handshakeManager.DeleteHostInfo(hostinfo)`, disrupting connectivity between two legitimately-authenticated Nebula nodes.
- Repeat this at will since nothing about the RecvError acceptance path requires possession of a valid CA-signed certificate or completion of a handshake — directly analogous to the Treasury bug where "anyone" (no access control) could mutate protocol state and disrupt legitimate operations.

This is a remote, unauthenticated denial-of-service against established tunnels — matching the "remote state poisoning" / DoS impact bar.

### Likelihood Explanation
Likelihood is moderated by two factors: (1) the sender's UDP source address must match the tunnel's currently-known remote address (requiring either address spoofing capability or a window where the remote hasn't been established yet), and (2) whether `accept_recv_error` acceptance is enabled for that peer (config-dependent, and per the 1.9.7 changelog note this path has already been tightened once due to security concerns, suggesting real-world exploitability was previously demonstrated/considered). The 32-bit `RemoteIndex` is also visible in cleartext to any on-path observer of a single packet, making index discovery non-blind for an on-path/off-path-with-spoofing attacker. This is a plausible, low-cost DoS against a running tunnel with no cryptographic material required.

### Recommendation
- Do not act on `RecvError` for a tunnel unless the packet is authenticated (e.g., require it be sent as an AEAD-protected message under the tunnel's session keys, similar to how `Message`/`Test` types are protected) rather than accepting it in the unencrypted/pre-auth dispatch branch in `outside.go`.
- If backward-compatible cleartext RecvError must be retained, require additional proof-of-knowledge beyond a spoofable source-address match before tearing down `hostinfo` and deleting pending handshake state, and rate-limit acceptance per remote index/address.
- Consider defaulting `accept_recv_error` to a stricter mode (e.g., `never` or a stricter conditional than "always") given this is a state-destructive action reachable pre-authentication.

### Proof of Concept
Conceptual PoC (network-level, mirroring the Treasury PoC's "unauthenticated actor mutates critical state" pattern):
1. Establish two legitimate Nebula nodes A and B with a completed handshake/tunnel (both hold valid CA-signed certs).
2. As an attacker with no CA-signed certificate, craft a UDP packet with header `Type = RecvError`, `RemoteIndex` set to A's known local index for the A↔B tunnel (observed via passive capture of the cleartext header, or brute-forced), and source address spoofed to match B's known UDP address (`hr`).
3. Send this packet to A.
4. `readOutsidePackets` → `handleRecvError` (outside.go:81-84, 541-575) matches the index to the hostinfo, sees `addr == hr`, and calls `f.closeTunnel(hostinfo)` plus `hm.DeleteHostInfo(hostinfo)` — tearing down the valid tunnel without any authentication, forcing A and B to re-handshake (repeatable DoS). [2](#0-1)

### Citations

**File:** outside.go (L75-84)
```go
	// Unencrypted packets
	switch h.Type {
	case header.Handshake:
		f.handshakeManager.HandleIncoming(via, packet, h)
		return

	case header.RecvError:
		f.handleRecvError(via.UdpAddr, h)
		return
	}
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

**File:** header/header.go (L91-98)
```go
type H struct {
	Version        uint8
	Type           MessageType
	Subtype        MessageSubType
	Reserved       uint16
	RemoteIndex    uint32
	MessageCounter uint64
}
```

**File:** header/header.go (L100-110)
```go
// Encode uses the provided byte array to encode the provided header values into.
// Byte array must be capped higher than HeaderLen or this will panic
func Encode(b []byte, v uint8, t MessageType, st MessageSubType, ri uint32, c uint64) []byte {
	b = b[:Len]
	b[0] = v<<4 | byte(t&0x0f)
	b[1] = byte(st)
	binary.BigEndian.PutUint16(b[2:4], 0)
	binary.BigEndian.PutUint32(b[4:8], ri)
	binary.BigEndian.PutUint64(b[8:16], c)
	return b
}
```

**File:** CHANGELOG.md (L130-130)
```markdown
- Add a config option to control accepting `recv_error` packets which defaults to `always`. (#1569)
```

**File:** CHANGELOG.md (L188-191)
```markdown
### Changed

- Disable sending `recv_error` messages when a packet is received outside the allowable counter window. (#1459)
- Improve error messages and remove some unnecessary fatal conditions in the Windows and generic udp listener. (#1453)
```
