### Title
Certificate revocation (blocklist/expiry) is only enforced on a lagging periodic timer, not on the data-plane fast path - ([File: connection_manager.go])

### Summary
The external report describes a class of bug where a security-relevant state variable (`V`/collateral ratio) is updated at one point in time while a compensating variable (`cashInFlight`) is updated asynchronously in a separate transaction, creating a window where the security check (price cap) is computed from stale/inconsistent state and is therefore less strict than intended. The reachable analog in nebula is that certificate revocation state (CA blocklist, expiry) is authoritative immediately inside `cert.CAPool`, but it is only *consulted* for already-established tunnels on a periodic timer in `connectionManager`, not on the per-packet data path (firewall/decrypt). This produces the same "lagging enforcement" pattern: the state that should immediately restrict traffic is updated, but the actual enforcement point trails behind, leaving a window where traffic is treated as trusted when it should not be.

### Finding Description
Certificate validity is verified once during the handshake via `certVerifier()`/`VerifyCertificate` [1](#0-0)  and the resulting `CachedCertificate` is stored on the `ConnectionState` (`peerCert`) for the life of the tunnel [2](#0-1) .

After that point, revalidation against the live CA pool (which includes the blocklist and expiry checks) only happens in `connectionManager.isInvalidCertificate`, which is invoked from `makeTrafficDecision` — itself driven by a periodic timer (`trafficTimer`/`checkInterval`), not by every packet [3](#0-2) [4](#0-3) .

Meanwhile, the actual data plane — `Firewall.Drop` and `ConnectionState.Decrypt`/`VerifyRelay` — never re-checks certificate validity/blocklist status per packet. `Firewall.Drop` relies on the cached `peerCert` for group/CA-name rule matching only, and once a tuple is admitted into conntrack it is fast-pathed via `inConns` without any CA pool re-verification unless the firewall *rule set itself* changes (`rulesVersion`) [5](#0-4) . `Decrypt`/`VerifyRelay` only check the replay window, never certificate state [6](#0-5) .

This mirrors the report's root cause exactly: one variable (the CA pool's blocklist/expiry state) can be updated instantly (e.g., an admin blocklists a fingerprint via `BlocklistFingerprint`/config reload in `cert/ca_pool.go`), but the dependent enforcement action (`isInvalidCertificate` tearing down the tunnel) is deferred to a lagging periodic check, and the enforcement point that actually gates traffic (firewall + decrypt) never consults that state at all on the hot path.

### Impact Explanation
Between the moment an operator blocklists a fingerprint or a certificate expires and the next `connectionManager` tick that calls `isInvalidCertificate`, an already-established tunnel to a peer holding that now-invalid/blocklisted certificate continues to pass and decrypt traffic through the firewall and data plane. This is a "remote state poisoning" style weakening: revocation is not honored immediately even though the authoritative state (the CA pool) already reflects it, exactly analogous to the reported CIF-vs-V staleness causing an incorrect (less strict) real-time security decision.

### Likelihood Explanation
This requires no attacker action beyond already holding an established tunnel at the time revocation state changes (expiry is deterministic and blocklisting is an administrative action) — no CA-signed certificate forgery is needed, and no malicious peer/lighthouse behavior is required, satisfying the reachable-without-CA-cert constraint. The likelihood of the lag mattering is bounded by the `checkInterval` cadence of the connection manager, so it is a real but time-bounded window rather than a permanent bypass.

### Recommendation
Re-validate the peer certificate (expiry + blocklist) directly on the data-plane hot path (or at minimum shorten/trigger it eagerly on blocklist/CA-pool mutation) rather than relying solely on the periodic `connectionManager` sweep, so revocation state changes are enforced immediately rather than lagging behind the next timer tick.

### Proof of Concept
Not applicable in ask-only mode; conceptually: (1) establish a tunnel and let handshake cache `peerCert`; (2) admin calls `BlocklistFingerprint` on the peer's fingerprint or the cert naturally expires; (3) observe that packets continue to flow through `Firewall.Drop`/`Decrypt` uninterrupted until the next `connectionManager.makeTrafficDecision` cycle invokes `isInvalidCertificate` and tears the tunnel down.

### Citations

**File:** handshake_manager.go (L1161-1166)
```go
// certVerifier returns a CertVerifier that validates certs against the current CA pool.
func (hm *HandshakeManager) certVerifier() handshake.CertVerifier {
	return func(c cert.Certificate) (*cert.CachedCertificate, error) {
		return hm.f.pki.GetCAPool().VerifyCertificate(time.Now(), c)
	}
}
```

**File:** connection_state.go (L17-27)
```go
type ConnectionState struct {
	eKey           noiseutil.CipherState
	dKey           noiseutil.CipherState
	myCert         cert.Certificate
	peerCert       *cert.CachedCertificate
	initiator      bool
	messageCounter atomic.Uint64
	window         *Bits
	decryptLock    sync.Mutex
	writeLock      sync.Mutex
}
```

**File:** connection_state.go (L61-108)
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

// VerifyRelay verifies AEAD protected (but not encrypted) relay frames. packet must be length-checked by the caller.
func (cs *ConnectionState) VerifyRelay(l *slog.Logger, messageCounter uint64, packet []byte, nb []byte) error {
	cs.decryptLock.Lock()
	result := cs.window.Check(l, messageCounter)
	cs.decryptLock.Unlock()
	if !result {
		return ErrAlreadySeen
	}

	signedPayload := packet[:len(packet)-cs.dKey.Overhead()]
	signatureValue := packet[len(packet)-cs.dKey.Overhead():]
	_, err := cs.dKey.DecryptDanger(nil, signedPayload, signatureValue, messageCounter, nb)
	if err != nil {
		return err
	}

	cs.decryptLock.Lock()
	result = cs.window.Update(l, messageCounter)
	cs.decryptLock.Unlock()
	if !result {
		return ErrAlreadySeen
	}

	return nil
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

**File:** firewall.go (L505-560)
```go
func (f *Firewall) inConns(fp firewall.Packet, h *HostInfo, caPool *cert.CAPool, localCache firewall.ConntrackCache) bool {
	if localCache != nil {
		if _, ok := localCache[fp]; ok {
			return true
		}
	}
	conntrack := f.Conntrack
	conntrack.Lock()

	// Purge every time we test
	ep, has := conntrack.TimerWheel.Purge()
	if has {
		f.evict(ep)
	}

	c, ok := conntrack.Conns[fp]

	if !ok {
		conntrack.Unlock()
		return false
	}

	if c.rulesVersion != f.rulesVersion {
		// This conntrack entry was for an older rule set, validate
		// it still passes with the current rule set
		table := f.OutRules
		if c.incoming {
			table = f.InRules
		}

		// We now know which firewall table to check against
		if !table.match(fp, c.incoming, h.ConnectionState.peerCert, caPool) {
			if f.l.Enabled(context.Background(), slog.LevelDebug) {
				h.logger(f.l).Debug("dropping old conntrack entry, does not match new ruleset",
					"fwPacket", fp,
					"incoming", c.incoming,
					"rulesVersion", f.rulesVersion,
					"oldRulesVersion", c.rulesVersion,
				)
			}
			delete(conntrack.Conns, fp)
			conntrack.Unlock()
			return false
		}

		if f.l.Enabled(context.Background(), slog.LevelDebug) {
			h.logger(f.l).Debug("keeping old conntrack entry, does match new ruleset",
				"fwPacket", fp,
				"incoming", c.incoming,
				"rulesVersion", f.rulesVersion,
				"oldRulesVersion", c.rulesVersion,
			)
		}

		c.rulesVersion = f.rulesVersion
	}
```
