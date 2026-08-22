### Title
Handshake retransmit replay can roam a peer's trusted remote address without fresh authentication when `preferred_ranges`/`local_range` is configured - (File: `handshake_manager.go`)

### Summary
`HandshakeManager.CheckAndComplete` treats a byte-identical stage-1 handshake packet as a legitimate retransmit (`ErrAlreadySeen`), and `handleCheckAndCompleteError` reacts to it by calling `existing.SetRemoteIfPreferred(f.hostMap, via)`. Because the "already seen" check is a raw `bytes.Equal` comparison on the cached handshake packet rather than a freshness/session-authenticated check, an attacker who has previously observed (sniffed) a valid stage-1 packet can replay it byte-for-byte from a spoofed UDP source address and, if that spoofed address falls inside the node's configured `preferred_ranges`/`local_range`, cause `HostInfo.remote` to be switched to the attacker-controlled address.

### Finding Description
In `handshake_manager.go`, `CheckAndComplete` compares the new packet's `HandshakePacket[stage]` bytes to every existing hostinfo's stored bytes for the same VPN address: [1](#0-0) 

If a match is found it returns `ErrAlreadySeen`, which is then handled in `handleCheckAndCompleteError`: [2](#0-1) 

`SetRemoteIfPreferred` (in `hostmap.go`) will unconditionally switch `HostInfo.remote` to `via.UdpAddr` if the current remote isn't already inside a preferred range and the new (attacker-spoofed) address is: [3](#0-2) 

Unlike the data-plane roaming path `handleHostRoaming` (`outside.go`), which only runs after a packet has been successfully AEAD-decrypted with the live session key (i.e. cryptographically fresh, authenticated traffic) and which enforces roam-suppression via `lastRoam`/`lastRoamRemote`, the `ErrAlreadySeen` path is reached purely from a raw byte comparison of a static, previously-transmitted handshake packet — no decryption, no counter/nonce freshness check, and no roam-suppression check on entry. Because a stage-1 IX handshake message is fully formed and self-contained (its authenticity comes from the embedded certificate/signature, not from the transport), an attacker who merely captured a copy of it off the wire can retransmit it as-is from any spoofed source address without needing the private key or CA.

The exploit requires:
1. The attacker has a byte-exact copy of a previously sent, valid stage-1 packet for that peer (obtained by passive observation, not through any cryptographic capability).
2. The victim already completed a handshake for that vpnAddr, so `existing` is present in `mainHostMap`.
3. The node has `preferred_ranges` (or the deprecated `local_range`) configured, and the attacker's spoofed source address falls inside one of those ranges while the current remote does not.

Reference (compare to the properly-gated roaming path): [4](#0-3) 

### Impact Explanation
If the preconditions hold, the responder's outbound path for that tunnel is switched to send subsequent encrypted traffic to the attacker-controlled/spoofed address instead of the legitimate peer, without any proof of freshness or possession of the session key. This is a remote-state (hostmap/remote-address) poisoning: it can be used to blackhole/redirect a tunnel's outbound data (denial of service against that peer) using only replayed public packet bytes and address spoofing — not a decryption or key-recovery bypass, since the attacker still cannot decrypt or forge subsequent ciphertext without the negotiated session key.

### Likelihood Explanation
Exploitability is conditional and narrow: it requires (a) the operator to have configured `preferred_ranges`/`local_range` (not default), (b) the attacker to have previously captured an exact copy of a valid stage-1 packet (passive sniffing capability, not crypto break), and (c) the ability to spoof a UDP source address that falls specifically inside the configured preferred range. Under these conditions the replay is trivially repeatable, since the check is a pure byte comparison against a cached packet, not a nonce/counter or session-authenticated freshness check.

### Recommendation
Do not let `ErrAlreadySeen` trigger `SetRemoteIfPreferred`/roaming at all, since a duplicate stage-1 packet only proves the sender possesses a copy of previously-transmitted bytes, not current control of that source address. If roaming on retransmit is desired, gate it behind the same freshness guarantees used for data-plane roaming (e.g., require a response to the `header.Test`/`TestRequest` probe from the new address before switching `HostInfo.remote`, rather than switching first and probing after), and apply the same roam-suppression checks (`lastRoam`/`lastRoamRemote`) used in `handleHostRoaming`.

### Proof of Concept
Integration test (extending `e2e/handshake_manager_test.go`'s `TestHandshakeAlreadySeenPreferredRemote` pattern):
1. Configure `them`'s node with `preferred_ranges` containing a CIDR, e.g. `203.0.113.0/24`.
2. Complete a normal handshake between `myControl` and `theirControl`; capture `msg1` bytes via `myControl.GetFromUDP`.
3. Record `theirControl`'s current `HostInfo.CurrentRemote` for `me`'s vpnAddr.
4. Re-inject the exact same `msg1` bytes into `theirControl` but wrapped with a spoofed source `netip.AddrPort` inside `203.0.113.0/24` (using the e2e router/`InjectUDPPacket` with a modified via address) instead of the real `myUdpAddr`.
5. Assert that `theirControl.GetHostInfoByVpnAddr(...).CurrentRemote` has changed to the spoofed address even though no cryptographically fresh packet was ever verified from that address, demonstrating the invariant violation ("peer addressing/roam only changes on cryptographically fresh verified traffic") is broken.

### Citations

**File:** handshake_manager.go (L438-444)
```go
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

**File:** hostmap.go (L787-823)
```go
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

**File:** outside.go (L264-292)
```go
func (f *Interface) handleHostRoaming(hostinfo *HostInfo, via ViaSender) {
	curRemote := hostinfo.GetRemote()
	if !via.IsRelayed && curRemote != via.UdpAddr {
		if !f.lightHouse.GetRemoteAllowList().AllowAll(hostinfo.vpnAddrs, via.UdpAddr.Addr()) {
			if f.l.Enabled(context.Background(), slog.LevelDebug) {
				hostinfo.logger(f.l).Debug("lighthouse.remote_allow_list denied roaming", "newAddr", via.UdpAddr)
			}
			return
		}

		if !hostinfo.lastRoam.IsZero() && via.UdpAddr == hostinfo.lastRoamRemote && time.Since(hostinfo.lastRoam) < RoamingSuppressSeconds*time.Second {
			if f.l.Enabled(context.Background(), slog.LevelDebug) {
				hostinfo.logger(f.l).Debug("Suppressing roam back to previous remote",
					"suppressSeconds", RoamingSuppressSeconds,
					"udpAddr", curRemote,
					"newAddr", via.UdpAddr,
				)
			}
			return
		}

		hostinfo.logger(f.l).Info("Host roamed to new udp ip/port.",
			"udpAddr", curRemote,
			"newAddr", via.UdpAddr,
		)
		hostinfo.lastRoam = time.Now()
		hostinfo.lastRoamRemote = curRemote
		hostinfo.SetRemote(via.UdpAddr)
	}
```
