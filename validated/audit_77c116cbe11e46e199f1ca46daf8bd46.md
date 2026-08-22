### Title
Handshake Retransmit Enables Remote-State Poisoning via Spoofed Preferred-Range Source — ([File: hostmap.go], [File: handshake_manager.go])

### Summary
Nebula's handshake retransmit path lets an on-path network attacker who has merely captured (not decrypted or forged) an initiator's stage-1 handshake packet resend those exact, unmodified bytes from a spoofed UDP source address. If that spoofed source falls inside the responder's configured `preferred_ranges`, the responder treats the resend as a legitimate roaming event and repoints the tunnel's `remote` address to the attacker's address — exactly the "voucher redeemed by the wrong party" bug class: a signed artifact (the stage-1 packet) that is not bound to the sender's address can be reused by anyone who has seen it on the wire, letting an attacker win a race against the legitimate host and hijack the tunnel's remote-address state before the real handshake completes.

### Finding Description
When a duplicate stage-1 handshake packet arrives for an already-completed hostinfo, `CheckAndComplete` returns `ErrAlreadySeen`, and `handleCheckAndCompleteError` calls `existing.SetRemoteIfPreferred(f.hostMap, via)`: [1](#0-0) 

`SetRemoteIfPreferred` only checks whether the *source address of the new packet* falls in the locally configured `preferred_ranges` — it performs no verification that this address actually belongs to, or was authorized by, the certificate holder that originally initiated the handshake: [2](#0-1) 

The comparison that triggers `ErrAlreadySeen` is a byte-for-byte match against previously stored handshake bytes: [3](#0-2) 

Critically, nothing in this path re-verifies that the packet's UDP source (`via.UdpAddr`) is the same as (or otherwise cryptographically tied to) the entity that produced the stage-1 bytes. The stage-1 packet — like a voucher — carries a valid signature/proof from the initiator's certificate, but that proof says nothing about which network address is allowed to deliver it. Anyone who has observed the wire bytes (via sniffing/MITM) can replay them from a different, self-chosen source address. If that address lands inside `preferred_ranges` (a very commonly configured "trusted" CIDR, e.g. an internal VPC range), the responder happily "roams" to it via `hostinfo.SetRemote(via.UdpAddr)`, redirecting where it will send the stage-2 response and, subsequently, connection tracking/roaming behavior for the tunnel.

This mirrors the reported voucher issue precisely: the protocol artifact (voucher / stage-1 packet) is valid and correctly signed, but it is not bound to the specific address of its intended user, so whoever "redeems" it first (attacker via a preferred spoofed source vs. the legitimate initiator's real source) wins, and the legitimate party's own in-flight handshake can be starved out or misdirected.

### Impact Explanation
This constitutes remote state poisoning of the `HostInfo.remote` field, which controls where the responder sends its stage-2 handshake response and all subsequent outbound tunnel traffic and roaming logic. An attacker positioned to observe/replay the initiator's handshake packet — without ever possessing a CA-signed certificate or private key — can force the responder to point traffic at an address of the attacker's choosing, disrupting legitimate tunnel establishment and enabling further traffic-interception opportunities during the race window before the real reply/roam is processed.

### Likelihood Explanation
Requires: (1) an attacker able to observe a stage-1 packet on the wire (on-path/MITM capability, no CA cert needed), and (2) `preferred_ranges` configured with a range the attacker can source traffic from. `preferred_ranges` is a commonly used feature for prioritizing internal/LAN addressing, making condition (2) realistic in many deployments. No cryptographic material needs to be forged — only capture-and-replay, which is a low-effort primitive for a network-positioned attacker.

### Recommendation
Bind the "preferred remote" roam decision to more than just raw source-address matching against `preferred_ranges`; e.g., only allow `SetRemoteIfPreferred`/roaming transitions in response to packets that are freshly validated as part of the *current* handshake sequence (not stale retransmits matched purely by byte equality), and/or require that the address change be corroborated by a subsequently-decrypted authenticated packet before permanently repointing `remote`.

### Proof of Concept
1. Legitimate initiator sends stage-1 handshake packet `msg1` to responder from address `A` (not in `preferred_ranges`).
2. Attacker, positioned on-path, captures `msg1` unmodified.
3. Responder completes handshake with initiator at address `A`, establishing `HostInfo` with `remote = A`.
4. Attacker replays the identical `msg1` bytes to the responder from spoofed source `B`, where `B ∈ preferred_ranges`.
5. `CheckAndComplete` matches the byte-identical packet and returns `ErrAlreadySeen`; `handleCheckAndCompleteError` calls `existing.SetRemoteIfPreferred`, which finds `B` preferred over `A` and calls `SetRemote(B)`.
6. The responder's tunnel `remote` is now `B`; subsequent traffic (e.g., a `TestRequest` and future encrypted flow) will be sent to attacker's address `B` instead of the legitimate initiator `A`.

### Citations

**File:** handshake_manager.go (L436-444)
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
```

**File:** handshake_manager.go (L1103-1113)
```go
	switch err {
	case ErrAlreadySeen:
		if existing.SetRemoteIfPreferred(f.hostMap, via) {
			f.SendMessageToVpnAddr(header.Test, header.TestRequest, hostinfo.vpnAddrs[0], []byte(""), make([]byte, 12, 12), make([]byte, mtu))
		}
		// Resend the original response. The peer is committed to that response's
		// ephemeral keys; a freshly-built one would have different keys and break
		// the tunnel even though both sides "completed" the handshake.
		if msg := existing.HandshakePacket[handshakePacketStage2]; msg != nil {
			hm.sendHandshakeResponse(via, msg, existing, true)
		}
```

**File:** hostmap.go (L785-823)
```go
// SetRemoteIfPreferred returns true if the remote was changed. The lastRoam
// time on the HostInfo will also be updated.
func (i *HostInfo) SetRemoteIfPreferred(hm *HostMap, via ViaSender) bool {
	if via.IsRelayed {
		return false
	}

	currentRemote := i.GetRemote()
	if !currentRemote.IsValid() {
		i.SetRemote(via.UdpAddr)
		return true
	}

	// NOTE: We do this loop here instead of calling `isPreferred` in
	// remote_list.go so that we only have to loop over preferredRanges once.
	newIsPreferred := false
	for _, l := range hm.GetPreferredRanges() {
		// return early if we are already on a preferred remote
		if l.Contains(currentRemote.Addr()) {
			return false
		}

		if l.Contains(via.UdpAddr.Addr()) {
			newIsPreferred = true
		}
	}

	if newIsPreferred {
		// Consider this a roaming event
		i.lastRoam = time.Now()
		i.lastRoamRemote = currentRemote

		i.SetRemote(via.UdpAddr)

		return true
	}

	return false
}
```
