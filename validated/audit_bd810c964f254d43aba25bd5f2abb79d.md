Based on the investigation, the closest reachable analog to the "AntiSandwich" bug class in Nebula is the roam‑suppression logic in `handleHostRoaming`, which is a stability mechanism whose own design prevents fast correction of a state it (unintentionally) allows an attacker to poison.

### Title
Roam-suppression window lets a spoofed/replayed packet lock a tunnel onto an attacker-chosen remote address, and the anti-flap mechanism itself blocks the legitimate correction - ([File: outside.go])

### Summary
`handleHostRoaming` updates a `HostInfo`'s trusted remote UDP address whenever a packet from a *new* source address decrypts successfully, and then arms a 2-second "suppression window" (`RoamingSuppressSeconds`) that blocks roaming *back* to whatever address was in use just before. Because the roam decision is keyed purely on the UDP source address of an already-successfully-decrypted packet (address is not itself authenticated data, it's transport metadata), any attacker who can get one already-valid ciphertext delivered to the victim from a spoofed/alternate source address can force a roam to that address. The very timer meant to stop flapping then actively suppresses the peer's own legitimate traffic from correcting the remote back for up to two seconds, mirroring the reported class of bug: a mechanism intended to stabilize state ends up making state manipulated by an outsider "sticky" and non-arbitrageable/non-correctable for a window.

### Finding Description
`handleHostRoaming` in [1](#0-0)  is invoked from `readOutsidePackets` immediately after `ConnectionState.Decrypt` succeeds, i.e., strictly after AEAD authentication of the payload, but the address used to decide roaming is the UDP datagram's source `via.UdpAddr`, which is unauthenticated transport metadata: [2](#0-1) 

The roam check only gates on `remote_allow_list` and then compares the incoming address to `hostinfo.lastRoamRemote` to decide whether to suppress: [3](#0-2) 

`RoamingSuppressSeconds` and the `lastRoam`/`lastRoamRemote` fields exist specifically to prevent flapping "due to packets already in flight": [4](#0-3) [5](#0-4) 

Because decryption success does not depend on the source address, any network-position attacker (on-path sniffer, or blind spoofer able to guess/observe one valid in-flight ciphertext) can deliver a duplicate/spoofed copy of a legitimate encrypted packet from an address of their choosing. If that spoofed copy reaches the victim before the real one (or is itself the one legitimately delivered but relayed by the attacker with a different apparent source), `SetRemote`/roam logic will move the tunnel's trusted remote to the attacker-controlled address: [6](#0-5) 

Once that happens, the anti-flap logic then actively works against recovery: any subsequent legitimate packet from the real peer's original address, arriving within `RoamingSuppressSeconds`, is treated as the "previous" (now stale/poisoned) remote and is suppressed rather than restoring the correct path: [7](#0-6) 

This is structurally the same failure mode as the reported `AntiSandwichHook` issue: a mechanism built to make state "sticky" (preventing rapid, legitimate reversion) is exploitable to make an attacker-induced bad state persist, because the mechanism cannot distinguish "flapping caused by attacker manipulation" from "flapping caused by benign in-flight packets," and by design refuses correction for a fixed window.

### Impact Explanation
While the tunnel is roamed to the attacker-chosen address, traffic to that peer is sent to an address the attacker controls or influences (subject to `remote_allow_list`), and the peer's genuine return path is suppressed for up to `RoamingSuppressSeconds` (2s), causing packet loss/misdirection and a denial-of-service window on every occurrence. Because this is retriggerable, an attacker positioned to repeatedly inject spoofed valid ciphertext can indefinitely keep a tunnel's remote pointed away from the legitimate endpoint, i.e., persistent remote-state poisoning without ever needing a CA-signed certificate or breaking the AEAD.

### Likelihood Explanation
Exploitation requires the attacker to get one already-valid, previously observed ciphertext datagram delivered to the victim node with a source address of the attacker's choosing (classic on-path relay/spoof, no decryption break needed) and that source address to pass `remote_allow_list` (which is often permissive/default-allow for public/roaming peers). This is a realistic capability for an on-path or same-broadcast-domain/NAT attacker, making likelihood moderate; it is bounded somewhat by replay-window rules for exact duplicate counters but not for a first delivery race or for values still inside the receive window.

### Recommendation
Do not let roaming decisions be driven solely by unauthenticated transport source address plus a fixed suppression timer. Consider: requiring additional corroboration (e.g., a fresh handshake/keepalive round trip) before trusting a roam to a brand-new address, shortening/adjusting the suppression window based on confidence, or documenting explicitly (as the report suggests for `AntiSandwichHook`) that `handleHostRoaming`/`RoamingSuppressSeconds` assumes a non-adversarial network path and is not a security boundary against on-path/spoofing attackers.

### Proof of Concept
1. Establish tunnel between `me` and `them`; capture one valid encrypted data packet `them -> me`.
2. From an attacker-controlled address `E` (allowed by `me`'s `remote_allow_list`), replay that captured ciphertext to `me`, spoofed as coming from `E`.
3. `me`'s `ConnectionState.Decrypt` succeeds (AEAD only checks integrity, not source address) — see [8](#0-7) .
4. `handleHostRoaming` sees `via.UdpAddr == E != curRemote`, passes the allow-list check, and roams `me`'s remote for `them` to `E`, recording `lastRoamRemote = them's real address` and `lastRoam = now` — [9](#0-8) .
5. Within the next 2 seconds, genuine packets that legitimately arrive from `them`'s real address are suppressed by the `lastRoamRemote`/`RoamingSuppressSeconds` check instead of restoring the correct remote — [7](#0-6) , so `me` continues sending to `E` until the window expires or another roam event occurs.

### Citations

**File:** outside.go (L124-136)
```go
	}

	out, err = hostinfo.ConnectionState.Decrypt(f.l, h.MessageCounter, out, packet, nb)
	if err != nil {
		if f.l.Enabled(context.Background(), slog.LevelDebug) {
			hostinfo.logger(f.l).Debug("Failed to decrypt packet", "error", err, "from", via, "header", h)
		}
		return
	}

	// Roam before we respond
	f.handleHostRoaming(hostinfo, via)
	f.connectionManager.In(hostinfo)
```

**File:** outside.go (L264-294)
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

}
```

**File:** hostmap.go (L33-35)
```go
// How long we should prevent roaming back to the previous IP.
// This helps prevent flapping due to packets already in flight
const RoamingSuppressSeconds = 2
```

**File:** hostmap.go (L275-276)
```go
	lastRoam       time.Time
	lastRoamRemote netip.AddrPort
```

**File:** hostmap.go (L776-783)
```go
// TODO: Maybe use ViaSender here?
func (i *HostInfo) SetRemote(remote netip.AddrPort) {
	// We copy here because we likely got this remote from a source that reuses the object
	if i.GetRemote() != remote {
		i.remote.Store(&remote)
		i.remotes.LearnRemote(i.vpnAddrs[0], remote)
	}
}
```

**File:** connection_state.go (L61-82)
```go
func (cs *ConnectionState) Decrypt(l *slog.Logger, messageCounter uint64, out []byte, packet []byte, nb []byte) ([]byte, error) {
	var err error
	cs.decryptLock.Lock()
	result := cs.window.Check(l, messageCounter)
	cs.decryptLock.Unlock()
	if !result {
		return nil, ErrAlreadySeen
	}

	out, err = cs.dKey.DecryptDanger(out, packet[:header.Len], packet[header.Len:], messageCounter, nb)
	if err != nil {
		return nil, err
	}

	cs.decryptLock.Lock()
	result = cs.window.Update(l, messageCounter)
	cs.decryptLock.Unlock()
	if !result {
		return nil, ErrAlreadySeen
	}
	return out, nil
}
```
