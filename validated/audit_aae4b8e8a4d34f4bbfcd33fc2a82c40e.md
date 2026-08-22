### Title
Replayed/spoofed data packet can poison the roam-suppression cooldown and block legitimate reconnection - (File: outside.go)

### Summary
`handleHostRoaming` in `outside.go` updates a hostinfo's trusted remote UDP address whenever a packet decrypts successfully from a new source address, and it uses a time-based cooldown (`lastRoam` / `lastRoamRemote` / `RoamingSuppressSeconds`) to suppress "flapping" back to a recently-replaced address. Because the roam decision is keyed only on the UDP source address of an already-decrypted packet, and a captured ciphertext can be replayed by a spoofed source before the real one arrives, an attacker who observes traffic (no CA-signed certificate required) can win the race, flip the hostinfo's remote to an address they control, and in doing so set `lastRoamRemote` to the legitimate peer's real address. The next genuine packet from the real peer is then treated as "roaming back to `lastRoamRemote`" and is suppressed for `RoamingSuppressSeconds`, blocking the tunnel from correcting itself — the same class of bug as the reported `checkTransaction` cooldown DoS: a value that gates/blocks a legitimate future action is reset by an unauthorized party.

### Finding Description
`ConnectionState.Decrypt` checks the replay window (`window.Check`) *before* decrypting and only marks the counter as seen (`window.Update`) *after* a successful decrypt: [1](#0-0) . This means the very first delivery of a given message counter — genuine or replayed — is accepted, whichever arrives first. If an attacker captures a legitimate ciphertext packet on the wire and re-sends it with a spoofed UDP source address before the real packet arrives, it will pass the replay check and decrypt successfully (since it is bit-for-bit the same authenticated ciphertext), racing the real packet.

Once decrypted, `readOutsidePackets` unconditionally calls `handleHostRoaming(hostinfo, via)`, where `via.UdpAddr` is simply the network-layer source address of the packet that made it through decryption: [2](#0-1) .

`handleHostRoaming` implements the "cooldown": [3](#0-2) 
- It compares `curRemote` (currently trusted remote) to `via.UdpAddr`.
- It checks `AllowAll` from the remote allow list (a static config-based CIDR filter, not a proof of identity).
- It checks `via.UdpAddr == hostinfo.lastRoamRemote && time.Since(hostinfo.lastRoam) < RoamingSuppressSeconds*time.Second` to suppress flapping back to a previous address.
- If not suppressed, it updates `lastRoam`, `lastRoamRemote = curRemote` (the *old* remote, about to be replaced), and calls `SetRemote(via.UdpAddr)`.

An attacker who replays a captured packet from a spoofed address `A` (attacker-controlled) while the hostinfo's current remote is the real peer address `R`:
1. Roam triggers: `lastRoamRemote` is set to `R` (the old, legitimate remote), and `SetRemote(A)` makes the tunnel start sending to the attacker's address.
2. When the real peer's next packet arrives from `R`, `via.UdpAddr == hostinfo.lastRoamRemote` (`R == R`) is true, and the suppression window blocks the roam back to `R` for `RoamingSuppressSeconds`.

The net effect: the legitimate peer's real address is treated as a suppressed "previous" address, and outbound Nebula traffic keeps going to the attacker-influenced address until the cooldown expires — exactly analogous to the reported bug where a publicly-reachable state (`_lastExecutedTx`) update blocked legitimate future calls to `checkTransaction`.

### Impact Explanation
This can be used to disrupt an established tunnel's connectivity (remote state poisoning / DoS of the roaming self-healing mechanism), without needing a CA-signed certificate — the attacker only needs to observe or predict a valid ciphertext packet and spoof a UDP source address, which is trivial on shared/broadcast networks or via basic on-path positioning. While the attacker cannot read or forge new plaintext traffic (Noise/AEAD is unbroken), they can misdirect where the responder sends its ciphertext and then use the suppression window to prevent the tunnel from self-correcting for `RoamingSuppressSeconds`, degrading or breaking connectivity for that duration.

### Likelihood Explanation
Requires: (a) ability to spoof a UDP source address (common outside strict egress-filtered networks or from an on-path position), and (b) capturing/knowing a valid, not-yet-delivered ciphertext frame to win a race against the genuine packet's delivery to the responder. This is a non-trivial but realistic on-path/near-path attack; it does not require possession of the tunnel's cryptographic keys or a certificate signed by the network's CA.

### Recommendation
Do not let an unauthenticated network-layer source address influence roam-suppression state based purely on a race against delivery order. Options:
- Only update `lastRoam`/`lastRoamRemote`/`SetRemote` after some additional corroboration (e.g., require multiple consecutive packets from the new source, or a successful round-trip test) rather than a single decrypted packet.
- Track suppression per learned-remote pair more conservatively, or shrink the window in which a "roam back" is blocked so a spoofed roam cannot lock out the legitimate address for a meaningful duration.
- Consider binding roam decisions to the actual UDP socket recvfrom results and any available anti-spoofing signal, and treat `RoamingSuppressSeconds` suppression as best-effort NAT-flap mitigation, not a security boundary.

### Proof of Concept
1. Attacker passively captures a valid Nebula ciphertext packet sent from real peer `R` to victim `V` (e.g., on a shared LAN/hub segment or by being on-path).
2. Before the genuine packet reaches `V`, attacker sends the identical bytes to `V` from a spoofed UDP source address `A`.
3. `V`'s `ConnectionState.Decrypt` window check passes (first time this counter is seen) [4](#0-3) , decrypt succeeds, and `handleHostRoaming` runs with `via.UdpAddr = A` [5](#0-4) .
4. `V` sets `lastRoamRemote = R`, `lastRoam = now`, and `SetRemote(A)` — outbound traffic to the tunnel now goes to `A`.
5. Shortly after, the real packet from `R` arrives (now flagged `ErrAlreadySeen` by the replay window and dropped as data, but any subsequent legitimate packet from `R` after the counter advances) triggers `handleHostRoaming` again; because `via.UdpAddr (R) == hostinfo.lastRoamRemote (R)` and the cooldown hasn't expired, the roam back to `R` is suppressed, and `V` continues sending to `A` for up to `RoamingSuppressSeconds`. [3](#0-2) [1](#0-0)

### Citations

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

**File:** outside.go (L126-136)
```go
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
