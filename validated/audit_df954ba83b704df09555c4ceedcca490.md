### Title
Unauthenticated attacker can front-run a pending handshake using the cleartext `RemoteIndex` to abort it before completion, causing a denial-of-service/state-poisoning loss for the victim - (File: `handshake_manager.go`)

### Summary
`nebula`'s wire header carries the handshake continuation index (`RemoteIndex`) in cleartext, before any certificate/CA verification takes place [1](#0-0) . `HandshakeManager.HandleIncoming` and `continueHandshake` route any inbound packet that carries a matching index straight into the pending `handshake.Machine`, with zero authentication of the sender [2](#0-1) [3](#0-2) . If that packet fails Noise processing and marks the Machine as unrecoverable, the code immediately deletes the victim's pending `HostInfo` [4](#0-3) . This is structurally the same "front-run to force a loss for a legitimate, in-flight operation" bug class described in the PoolTogether report: there, a claimer's batched claim could be pre-empted by anyone racing the last prize in the list, causing the legitimate bot's later processing to fail and be wasted. Here, an attacker who is not a member of the mesh and holds no CA-signed certificate can race the legitimate responder's reply and destroy the victim's in-progress handshake before it completes.

### Finding Description
The Nebula wire header is fixed at 16 bytes and always sent in cleartext for handshake packets: `Version|Type|Subtype|Reserved|RemoteIndex(uint32)|MessageCounter(uint64)` [5](#0-4) [6](#0-5) . `RemoteIndex` is the value the recipient uses to look up its own pending state; for a stage‑2 response sent back to an initiator, this field is the initiator's own locally-generated index, and it is emitted on the wire unauthenticated and unencrypted.

`HandshakeManager.HandleIncoming` demonstrates this: stage‑1 packets are handled by `beginHandshake` (createing a brand-new Machine), but any continuation packet is matched purely by `h.RemoteIndex` via `hm.queryIndex`, with no certificate check at this point: [7](#0-6) 

`continueHandshake` then feeds the raw packet bytes directly into `machine.ProcessPacket`: [3](#0-2) 

If Noise processing fails and the Machine reports `Failed() == true`, the handler unconditionally calls `hm.DeleteHostInfo(hostinfo)`, discarding the entire pending handshake state: [4](#0-3) 

Because `RemoteIndex` travels in cleartext (it is not part of any Noise-encrypted payload), any attacker capable of observing the legitimate responder's stage‑2 packet on the wire — e.g. a network-position attacker, a malicious router, or an ARP/L2 spoofer — learns the exact index value needed to target the victim's pending Machine. That attacker does not need, and never presents, a CA-signed certificate: `beginHandshake`/`continueHandshake` accept and process the packet before `certVerifier` is invoked, and `queryIndex` matching happens purely on the numeric index, independent of source-address or certificate authenticity [8](#0-7) .

By spoofing a UDP packet carrying the observed `RemoteIndex` with garbage/invalid Noise payload and delivering it to the victim before the genuine stage‑2 response arrives, the attacker "front-runs" the real reply. The victim's Machine is fed the malformed packet, fails, and its `HostInfo` is deleted — destroying the legitimate handshake attempt and forcing a costly restart, exactly analogous to the batched-claim front-run in the referenced report where a malicious actor pre-empts the last prize claim to force the legitimate claimer's batched transaction to fail and waste gas.

### Impact Explanation
This is a remote, unauthenticated denial-of-service / state-poisoning primitive against tunnel establishment:
- An attacker with no valid CA-signed certificate can repeatedly abort in-flight handshakes between two legitimate mesh members purely by racing/observing the cleartext index, without ever completing authentication.
- This forces continuous re-handshaking, delaying tunnel establishment and creating churn (`hm.DeleteHostInfo` plus a fresh handshake attempt), which can be weaponized to keep two nodes from ever completing a handshake as long as the attacker can win the race consistently.
- It maps to the "remote state poisoning" / "remote crash (of a specific handshake state) impact" category: the pending `HostInfo` and `Machine` for a legitimate initiator is destroyed by an unauthenticated party's malformed packet.

### Likelihood Explanation
Likelihood depends on the attacker's ability to observe the cleartext stage‑2 `RemoteIndex` before the real response reaches the initiator and then win the race by delivering a spoofed UDP packet first. This is plausible for on-path attackers (shared LAN/WiFi, compromised routers, ISPs) and is comparable in nature to the original UDP-based front-run scenario: no cryptographic secret protects the index value, and the matching/`ProcessPacket` path performs no source authentication prior to failure-triggered deletion.

### Recommendation
- Do not delete pending handshake state on a single malformed continuation packet matched only by cleartext index. Require additional binding (e.g. rate-limited retries, requiring multiple consistent failures from the same claimed remote, or deferring `DeleteHostInfo` until a timeout) before abandoning a pending handshake.
- Consider treating a `ProcessPacket` failure on a continuation packet as "drop and wait for retransmit/timeout" rather than immediately calling `DeleteHostInfo`, unless the packet can be tied to genuine, verified failure (not just any packet bearing a guessable/observable index).
- Where feasible, ensure indices used for handshake-state lookup are not solely reliant on cleartext header fields for consequential state-mutating actions (deletion), or add a lightweight proof-of-origin check before honoring a failure-triggering packet.

### Proof of Concept
1. Node A (initiator, no attacker cert needed to observe) begins a handshake toward Node B; A's local index is only known once B's stage‑2 response is observed on the wire (cleartext `RemoteIndex` field, `header.go` bytes 4-8).
2. An on-path attacker C (holding no CA-signed certificate) observes B's real stage‑2 UDP packet in flight to A and extracts `RemoteIndex` from the cleartext header.
3. C crafts and sends a spoofed UDP packet to A with the same `RemoteIndex`, `Type=Handshake`, `Subtype=HandshakeIXPSK0`, and malformed/garbage Noise payload, timed to arrive before B's genuine stage‑2 response (front-running).
4. A's `HandleIncoming` → `queryIndex` matches C's spoofed packet to A's pending `HandshakeHostInfo` and calls `continueHandshake` → `machine.ProcessPacket`, which fails Noise decoding and sets `Failed()=true` [4](#0-3) .
5. A calls `hm.DeleteHostInfo(hostinfo)`, discarding the pending state. When B's legitimate stage‑2 packet subsequently arrives, A no longer has the matching pending Machine/index, so the genuine handshake is dropped as an orphaned packet, forcing A and B to restart the handshake — a loss imposed on legitimate parties by an attacker who never held a valid certificate.

### Citations

**File:** header/header.go (L10-27)
```go
//Version 1 header:
// 0                                                                       31
// |-----------------------------------------------------------------------|
// | Version (uint4) | Type (uint4) |  Subtype (uint8) | Reserved (uint16) | 32
// |-----------------------------------------------------------------------|
// |                        Remote index (uint32)                          | 64
// |-----------------------------------------------------------------------|
// |                           Message counter                             | 96
// |                               (uint64)                                | 128
// |-----------------------------------------------------------------------|
// |                               payload...                              |

type m = map[string]any

const (
	Version uint8 = 1
	Len           = 16
)
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

**File:** header/header.go (L142-156)
```go
// Parse is a helper function to parses given bytes into new Header struct
func (h *H) Parse(b []byte) error {
	if len(b) < Len {
		return ErrHeaderTooShort
	}
	// get upper 4 bytes
	h.Version = uint8((b[0] >> 4) & 0x0f)
	// get lower 4 bytes
	h.Type = MessageType(b[0] & 0x0f)
	h.Subtype = MessageSubType(b[1])
	h.Reserved = binary.BigEndian.Uint16(b[2:4])
	h.RemoteIndex = binary.BigEndian.Uint32(b[4:8])
	h.MessageCounter = binary.BigEndian.Uint64(b[8:16])
	return nil
}
```

**File:** handshake_manager.go (L186-193)
```go

	// Continuation message must match a pending handshake by index.
	// Anything else is an orphaned packet (e.g., late retransmit after
	// timeout) and is dropped.
	if hh := hm.queryIndex(h.RemoteIndex); hh != nil {
		hm.continueHandshake(via, hh, packet)
		return
	}
```

**File:** handshake_manager.go (L812-860)
```go
// continueHandshake feeds an incoming packet to an existing pending handshake Machine.
func (hm *HandshakeManager) continueHandshake(via ViaSender, hh *HandshakeHostInfo, packet []byte) {
	f := hm.f

	hh.Lock()
	defer hh.Unlock()

	// Re-verify hh is still tracked. Between queryIndex returning and us taking
	// hh.Lock, handleOutbound may have timed out and deleted it. Once we hold
	// hh.Lock no other deleter can race our index: handleOutbound also takes
	// hh.Lock first, and handleRecvError targets a main-hostmap entry with a
	// different localIndexId.
	hm.RLock()
	cur, ok := hm.indexes[hh.hostinfo.localIndexId]
	hm.RUnlock()
	if !ok || cur != hh {
		return
	}

	hostinfo := hh.hostinfo
	if !via.IsRelayed {
		if !f.lightHouse.GetRemoteAllowList().AllowAll(hostinfo.vpnAddrs, via.UdpAddr.Addr()) {
			f.l.Debug("lighthouse.remote_allow_list denied incoming handshake",
				"vpnAddrs", hostinfo.vpnAddrs, "from", via)
			return
		}
	}

	machine := hh.machine
	if machine == nil {
		f.l.Error("No handshake machine available for continuation",
			"vpnAddrs", hostinfo.vpnAddrs, "from", via)
		hm.DeleteHostInfo(hostinfo)
		return
	}

	response, result, err := machine.ProcessPacket(nil, packet)
	if err != nil {
		// Recoverable errors are routine noise, log at Debug. Fatal errors get a Warn.
		if machine.Failed() {
			f.l.Warn("Failed to process handshake packet, abandoning",
				"vpnAddrs", hostinfo.vpnAddrs, "from", via, "error", err)
			hm.DeleteHostInfo(hostinfo)
		} else {
			f.l.Debug("Failed to process handshake packet",
				"vpnAddrs", hostinfo.vpnAddrs, "from", via, "error", err)
		}
		return
	}
```
