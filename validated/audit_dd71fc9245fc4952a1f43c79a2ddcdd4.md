### Title
Unthrottled remote-roam via replayed handshake packets bypasses the roaming suppression/cooldown - (File: `handshake_manager.go`, `hostmap.go`)

### Summary
The `ErrAlreadySeen` branch of the handshake state machine lets an attacker who merely replays a previously‑observed (captured) handshake byte-string from a spoofed UDP source repeatedly force an established tunnel to "roam" its trusted remote address to that spoofed address. Unlike the data-plane roaming path, this handshake-layer roaming call is not protected by the anti-flapping cooldown that exists elsewhere in the codebase, so it can be invoked over and over with no penalty — the same "cheap, repeatable state manipulation with no cost" pattern described in the external report, just applied to Nebula's peer/remote-address trust state instead of a staking balance.

### Finding Description
When a duplicate/replayed handshake stage-1 (`msg1`) packet arrives, `HandshakeManager.CheckAndComplete` detects the byte-for-byte match and returns `ErrAlreadySeen`: [1](#0-0) 

`handleCheckAndCompleteError` then reacts to `ErrAlreadySeen` by calling `existing.SetRemoteIfPreferred(f.hostMap, via)`, using the *source address of the just-arrived (replayed) packet* as the candidate new remote, entirely independent of any certificate or session decryption: [2](#0-1) 

`SetRemoteIfPreferred` will switch the hostinfo's trusted remote to that address as long as it falls in a configured "preferred range" (e.g. a LAN CIDR) and the current remote isn't already preferred: [3](#0-2) 

Crucially, this call path has **no cooldown/anti-flap guard**. Compare with the data-plane roaming path `handleHostRoaming`, which explicitly enforces `RoamingSuppressSeconds` before allowing another roam back to a recently-seen remote: [4](#0-3) 

`SetRemoteIfPreferred` does not consult `lastRoam`/`lastRoamRemote` at all before switching, so nothing stops the exact same trigger (a replayed msg1) from being re-fired arbitrarily fast to move the "trusted remote" pointer over and over. An attacker requires no CA-signed certificate for this: `msg1` is the initiator's unauthenticated first Noise message, observable/capturable by any network-adjacent listener, and its exact bytes plus an arbitrary spoofed UDP source (feasible whenever the attacker shares a "preferred" L2/L3 segment with a victim) are sufficient to repeatedly re-trigger the `ErrAlreadySeen` → `SetRemoteIfPreferred` path.

This is the same bug class as the reported gauge issue: a cheap, repeatable action (replaying a captured packet from a different spoofed source) that mutates trusted state (`hostinfo.remote`) with no minimum interval or penalty, letting the attacker win a race against genuine roaming/traffic and redirect where the peer's outbound ciphertext is sent.

### Impact Explanation
If successfully raced, `hostinfo.remote` is repointed to an attacker-chosen UDP endpoint within the victim's preferred ranges. All subsequent outbound (encrypted) traffic for that tunnel is sent to the attacker's address instead of the legitimate peer's, i.e. remote state poisoning / traffic redirection of an established tunnel, achievable by an attacker who never obtained a CA-signed certificate. Because there is no rate limit on this action (no consultation of `lastRoam`), the attacker can keep re-asserting control of the remote pointer indefinitely, out-competing legitimate roam/rebind events, matching the "no minimum period / no penalty" flaw class from the report.

### Likelihood Explanation
Exploitation requires the attacker to (a) capture one legitimate `msg1` packet (trivially observable on shared media / off-path sniffing scenarios) and (b) be able to send spoofed UDP packets with a source address inside one of the victim's configured `preferred_ranges` (commonly broad LAN CIDRs). Both conditions are realistic for an on-path/adjacent-network attacker with no certificate at all, making this moderately likely in deployments using non-trivial `preferred_ranges`.

### Recommendation
Apply the same `RoamingSuppressSeconds`-style cooldown/anti-flap check (or a rate limiter keyed by vpn address / hostinfo) inside `SetRemoteIfPreferred`, or gate the `ErrAlreadySeen`-triggered roam so it cannot fire more often than the existing roam-suppression window used by `handleHostRoaming`. Additionally consider requiring that the replayed packet's declared endpoint be corroborated by more than exact-byte packet replay (e.g., ignore roam candidates from `ErrAlreadySeen` entirely, since this path is meant only for retransmission handling, not roaming).

### Proof of Concept
1. Legitimate peers `me` and `them` complete a handshake; `me`'s remote for `them` is set to `them`'s real address.
2. Attacker, sharing a "preferred" CIDR with `me`, captures `them`'s original `msg1` (or any valid captured handshake packet addressed to `me`).
3. Attacker repeatedly sends the exact same captured bytes to `me` from different spoofed source addresses within the preferred range, e.g. every few milliseconds.
4. Each delivery lands in `CheckAndComplete` → `ErrAlreadySeen` → `handleCheckAndCompleteError` → `SetRemoteIfPreferred`, and because there is no cooldown check, `me`'s `hostinfo.remote` for `them` is repeatedly reset to the attacker's latest spoofed source, overriding legitimate rebinds/roams.
5. `me`'s subsequent outbound ciphertext for the tunnel is now sent to the attacker-controlled address. [5](#0-4) [6](#0-5)

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
