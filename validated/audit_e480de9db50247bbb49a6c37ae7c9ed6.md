### Title
Certificate revocation/expiry (blocklist, expiry) is enforced only by an asynchronous periodic timer, not at packet-processing time — allowing traffic to keep flowing through the firewall for a full check-interval window after a peer's trust should have ended - (File: `connection_manager.go`, `firewall.go`, `outside.go`, `inside.go`)

### Summary
The Sherlock report's root cause is that a security-relevant state transition (partyA liquidation) does not immediately invalidate credentials that continue to authorize sensitive actions (`deallocateForPartyB` / `transferAllocation`) on the peer side; the check for "is this still allowed" is decoupled in time from the action itself, and the intervening event never bumps the value the authorization is bound to. The reachable analog in nebula is the way certificate validity/blocklist status is (re)checked: `Firewall.Drop` (`firewall.go:425-479`) and the encrypt/decrypt data path (`outside.go`, `inside.go`) never re-verify the peer certificate against the CA pool (expiry, blocklist) on a per-packet basis. That verification only happens out-of-band, on the `connectionManager`'s periodic ticker, via `isInvalidCertificate` [1](#0-0) , invoked from `makeTrafficDecision` [2](#0-1) .

### Finding Description
`Firewall.Drop` decides whether to allow a packet using the cached `*cert.CachedCertificate` stored on `HostInfo.ConnectionState.peerCert`, matching it against firewall rules (`table.match`) and conntrack state — it never calls `caPool.VerifyCachedCertificate`/`VerifyCertificate` to check expiry or blocklist status: [3](#0-2) . Both the outbound path (`consumeInsidePacket` in `inside.go`) and inbound path (`readOutsidePackets`/`handleOutsideMessagePacket` in `outside.go`) call `Firewall.Drop` directly for every packet without any certificate-freshness check: [4](#0-3) [5](#0-4) .

The only place a peer's certificate is re-validated against the current CA pool (to catch expiry or a newly-added blocklist entry) is `isInvalidCertificate`, which is driven exclusively by the `connectionManager`'s `trafficTimer` wheel and fires at most once per `checkInterval` (default `timers.connection_alive_interval` = 5s) per tracked hostinfo, or `pendingDeletionInterval` (default 10s) for idle tunnels: [6](#0-5) [7](#0-6) . This is structurally identical to the reported bug class: the entity that should lose its authorized status (partyA being liquidated / here, a peer whose cert is blocklisted or has expired) can still have actions performed against it (data-plane traffic forwarded, firewall-rule matches evaluated using its now-stale credential) because the "revocation" check is decoupled from the action path and only catches up asynchronously.

### Impact Explanation
Once an operator blocklists a certificate fingerprint (e.g., after discovering a compromised host key) or a certificate simply expires, the local `Firewall.Drop`/data-plane path keeps forwarding and firewall-rule-matching traffic for that peer's existing tunnel(s) until the next scheduled `connectionManager` tick fires `closeTunnel`. This is a window (bounded by `checkInterval`, but can be longer for idle tunnels bounded by `pendingDeletionInterval`, and subject to scheduler/goroutine latency under load) during which a revoked/expired identity retains full data-plane trust and firewall-group membership for `group`/`ca_name`/`ca_sha` rule matching, i.e. a temporal certificate-verification bypass on the enforcement path.

### Likelihood Explanation
This requires no special access beyond already being a valid (but now-revoked/expiring) mesh peer with an established tunnel — exactly the "no CA-signed certificate held by the attacker" constraint, since the attacker already possesses their own (soon-to-be-invalid) certificate and simply needs the tunnel to remain in the hostmap. No collusion of multiple privileged roles is needed, unlike the original DeFi report; the trigger event (blocklist push or natural cert expiry) is entirely under the protocol operator/CA, not the attacker, which somewhat lowers likelihood, but the exploitation window is deterministic and always present by design (not an edge case), which raises it.

### Recommendation
Perform (or cache the result of) certificate/CA-pool validity and blocklist checks in the packet-processing hot path itself — e.g., have `Firewall.Drop` consult a cheap, frequently-refreshed "is this hostinfo's peer cert currently valid" flag that is updated immediately when `cert.CAPool.BlocklistFingerprint` is called or a reload occurs, rather than relying solely on the `connectionManager`'s multi-second polling interval. At minimum, reduce and clearly document the maximum enforcement-lag window, and trigger an out-of-band immediate re-check of all hostinfos when the blocklist changes instead of waiting for the next scheduled tick.

### Proof of Concept
Not directly executable from the indexed context (no test harness wiring blocklist updates to an active data-plane send loop was found), but the logical PoC is:
1. Establish a tunnel between A and B; B's certificate is currently valid.
2. Operator calls `CAPool.BlocklistFingerprint(B's fingerprint)` (or B's cert naturally expires) mid-session.
3. Immediately (before the next `checkInterval` tick) have B send/receive further data-plane packets through A — `Firewall.Drop` will still evaluate them against the stale `ConnectionState.peerCert` and forward them, since neither `inside.go`/`outside.go` nor `firewall.go` call into `caPool.VerifyCachedCertificate` per packet; the tear-down only happens once `isInvalidCertificate` is scheduled and run by `connectionManager.doTrafficCheck` [8](#0-7) .

### Citations

**File:** connection_manager.go (L67-78)
```go
func (cm *connectionManager) reload(c *config.C, initial bool) {
	if initial {
		cm.checkInterval = time.Duration(c.GetInt("timers.connection_alive_interval", 5)) * time.Second
		cm.pendingDeletionInterval = time.Duration(c.GetInt("timers.pending_deletion_interval", 10)) * time.Second

		// We want at least a minimum resolution of 500ms per tick so that we can hit these intervals
		// pretty close to their configured duration.
		// The inactivity duration is checked each time a hostinfo ticks through so we don't need the wheel to contain it.
		minDuration := min(time.Millisecond*500, cm.checkInterval, cm.pendingDeletionInterval)
		maxDuration := max(cm.checkInterval, cm.pendingDeletionInterval)
		cm.trafficTimer = NewLockingTimerWheel[uint32](minDuration, maxDuration)
	}
```

**File:** connection_manager.go (L166-192)
```go
func (cm *connectionManager) doTrafficCheck(localIndex uint32, p, nb, out []byte, now time.Time) {
	decision, hostinfo, primary := cm.makeTrafficDecision(localIndex, now)

	switch decision {
	case deleteTunnel:
		if cm.hostMap.DeleteHostInfo(hostinfo) {
			// Only clearing the lighthouse cache if this is the last hostinfo for this vpn ip in the hostmap
			cm.intf.lightHouse.DeleteVpnAddrs(hostinfo.vpnAddrs)
		}

	case closeTunnel:
		cm.intf.sendCloseTunnel(hostinfo)
		cm.intf.closeTunnel(hostinfo)

	case swapPrimary:
		cm.swapPrimary(hostinfo, primary)

	case migrateRelays:
		cm.migrateRelayUsed(hostinfo, primary)

	case tryRehandshake:
		cm.tryRehandshake(hostinfo)

	case sendTestPacket:
		cm.intf.SendMessageToHostInfo(header.Test, header.TestRequest, hostinfo, p, nb, out)
	}

```

**File:** connection_manager.go (L311-324)
```go
func (cm *connectionManager) makeTrafficDecision(localIndex uint32, now time.Time) (trafficDecision, *HostInfo, *HostInfo) {
	// Read lock the main hostmap to order decisions based on tunnels being the primary tunnel
	cm.hostMap.RLock()
	defer cm.hostMap.RUnlock()

	hostinfo := cm.hostMap.Indexes[localIndex]
	if hostinfo == nil {
		cm.l.Debug("Not found in hostmap", "localIndex", localIndex)
		return doNothing, nil, nil
	}

	if cm.isInvalidCertificate(now, hostinfo) {
		return closeTunnel, hostinfo, nil
	}
```

**File:** connection_manager.go (L422-420)
```go

```

**File:** connection_manager.go (L470-500)
```go
// isInvalidCertificate decides if we should destroy a tunnel.
// returns true if pki.disconnect_invalid is true and the certificate is no longer valid.
// Blocklisted certificates will skip the pki.disconnect_invalid check and return true.
func (cm *connectionManager) isInvalidCertificate(now time.Time, hostinfo *HostInfo) bool {
	remoteCert := hostinfo.GetCert()
	if remoteCert == nil {
		return false //don't tear down tunnels for handshakes in progress
	}

	caPool := cm.intf.pki.GetCAPool()
	err := caPool.VerifyCachedCertificate(now, remoteCert)
	if err == nil {
		return false //cert is still valid! yay!
	} else if err == cert.ErrBlockListed { //avoiding errors.Is for speed
		// Block listed certificates should always be disconnected
		hostinfo.logger(cm.l).Info("Remote certificate is blocked, tearing down the tunnel",
			"error", err,
			"fingerprint", remoteCert.Fingerprint,
		)
		return true
	} else if cm.intf.disconnectInvalid.Load() {
		hostinfo.logger(cm.l).Info("Remote certificate is no longer valid, tearing down the tunnel",
			"error", err,
			"fingerprint", remoteCert.Fingerprint,
		)
		return true
	} else {
		//if we reach here, the cert is no longer valid, but we're configured to keep tunnels from now-invalid certs open
		return false
	}
}
```

**File:** firewall.go (L425-478)
```go
func (f *Firewall) Drop(fp firewall.Packet, incoming bool, h *HostInfo, caPool *cert.CAPool, localCache firewall.ConntrackCache) error {
	// Make sure remote address matches nebula certificate, and determine how to treat it
	if h.networks == nil {
		// Simple case: Certificate has one address and no unsafe networks
		if h.vpnAddrs[0] != fp.RemoteAddr {
			f.metrics(incoming).droppedRemoteAddr.Inc(1)
			return ErrInvalidRemoteIP
		}
	} else {
		nwType, ok := h.networks.Lookup(fp.RemoteAddr)
		if !ok {
			f.metrics(incoming).droppedRemoteAddr.Inc(1)
			return ErrInvalidRemoteIP
		}
		switch nwType {
		case NetworkTypeVPN:
			break // nothing special
		case NetworkTypeVPNPeer:
			f.metrics(incoming).droppedRemoteAddr.Inc(1)
			return ErrPeerRejected // reject for now, one day this may have different FW rules
		case NetworkTypeUnsafe:
			break // nothing special, one day this may have different FW rules
		default:
			f.metrics(incoming).droppedRemoteAddr.Inc(1)
			return ErrUnknownNetworkType //should never happen
		}
	}

	// Make sure we are supposed to be handling this local ip address
	if !f.routableNetworks.Contains(fp.LocalAddr) {
		f.metrics(incoming).droppedLocalAddr.Inc(1)
		return ErrInvalidLocalIP
	}

	// Check if we spoke to this tuple, if we did then allow this packet
	if f.inConns(fp, h, caPool, localCache) {
		return nil
	}

	table := f.OutRules
	if incoming {
		table = f.InRules
	}

	// We now know which firewall table to check against
	if !table.match(fp, incoming, h.ConnectionState.peerCert, caPool) {
		f.metrics(incoming).droppedNoRule.Inc(1)
		return ErrNoMatchingRule
	}

	// We always want to conntrack since it is a faster operation
	f.addConn(fp, incoming)

	return nil
```

**File:** inside.go (L15-87)
```go
func (f *Interface) consumeInsidePacket(packet []byte, fwPacket *firewall.Packet, nb, out []byte, q int, localCache firewall.ConntrackCache) {
	err := newPacket(packet, false, fwPacket)
	if err != nil {
		if f.l.Enabled(context.Background(), slog.LevelDebug) {
			f.l.Debug("Error while validating outbound packet",
				"packet", packet,
				"error", err,
			)
		}
		return
	}

	// Ignore local broadcast packets
	if f.dropLocalBroadcast {
		if f.myBroadcastAddrsTable.Contains(fwPacket.RemoteAddr) {
			return
		}
	}

	if f.myVpnAddrsTable.Contains(fwPacket.RemoteAddr) {
		// Immediately forward packets from self to self.
		// This should only happen on Darwin-based and FreeBSD hosts, which
		// routes packets from the Nebula addr to the Nebula addr through the Nebula
		// TUN device.
		if immediatelyForwardToSelf {
			_, err := f.readers[q].Write(packet)
			if err != nil {
				f.l.Error("Failed to forward to tun", "error", err)
			}
		}
		// Otherwise, drop. On linux, we should never see these packets - Linux
		// routes packets from the nebula addr to the nebula addr through the loopback device.
		return
	}

	// Ignore multicast packets
	if f.dropMulticast && fwPacket.RemoteAddr.IsMulticast() {
		return
	}

	hostinfo, ready := f.getOrHandshakeConsiderRouting(fwPacket, func(hh *HandshakeHostInfo) {
		hh.cachePacket(f.l, header.Message, 0, packet, f.sendMessageNow, f.cachedPacketMetrics)
	})

	if hostinfo == nil {
		f.rejectInside(packet, out, q)
		if f.l.Enabled(context.Background(), slog.LevelDebug) {
			f.l.Debug("dropping outbound packet, vpnAddr not in our vpn networks or in unsafe networks",
				"vpnAddr", fwPacket.RemoteAddr,
				"fwPacket", fwPacket,
			)
		}
		return
	}

	if !ready {
		return
	}

	dropReason := f.firewall.Drop(*fwPacket, false, hostinfo, f.pki.GetCAPool(), localCache)
	if dropReason == nil {
		f.sendNoMetrics(header.Message, 0, hostinfo.ConnectionState, hostinfo, netip.AddrPort{}, packet, nb, out, q)

	} else {
		f.rejectInside(packet, out, q)
		if f.l.Enabled(context.Background(), slog.LevelDebug) {
			hostinfo.logger(f.l).Debug("dropping outbound packet",
				"fwPacket", fwPacket,
				"reason", dropReason,
			)
		}
	}
}
```

**File:** outside.go (L492-514)
```go
func (f *Interface) handleOutsideMessagePacket(hostinfo *HostInfo, out []byte, packet []byte, fwPacket *firewall.Packet, nb []byte, q int, localCache firewall.ConntrackCache) {
	err := newPacket(out, true, fwPacket)
	if err != nil {
		hostinfo.logger(f.l).Warn("Error while validating inbound packet",
			"error", err,
			"packet", out,
		)
		return
	}

	dropReason := f.firewall.Drop(*fwPacket, true, hostinfo, f.pki.GetCAPool(), localCache)
	if dropReason != nil {
		// NOTE: We give `packet` as the `out` here since we already decrypted from it and we don't need it anymore
		// This gives us a buffer to build the reject packet in
		f.rejectOutside(out, hostinfo.ConnectionState, hostinfo, nb, packet, q)
		if f.l.Enabled(context.Background(), slog.LevelDebug) {
			hostinfo.logger(f.l).Debug("dropping inbound packet",
				"fwPacket", fwPacket,
				"reason", dropReason,
			)
		}
		return
	}
```
