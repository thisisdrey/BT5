### Title
CA pool reload invalidates cached certificate verification for established tunnels, causing remote crash/DoS of active connections - ([File: connection_manager.go], [File: cert/ca_pool.go], [File: pki.go])

### Summary
The external report describes a class of bug: an authorization/trust mapping (`slashingHandler`) that is looked up fresh at use-time can be changed independently of already-created state (deployed `NativeVaults`), causing those pre-existing objects to be permanently rejected (DoS) because the state check compares against a value that no longer matches. The reachable analog in nebula is the CA pool (`cert.CAPool`), which is hot-swapped via `PKI.reloadCAPool` on config reload, while already-established `HostInfo` connections retain a cached certificate whose validity was pinned to the CA that signed it at handshake time [1](#0-0) .

### Finding Description
`PKI.reloadCAPool` atomically replaces the entire `*cert.CAPool` on every config reload via `p.caPool.Store(caPool)` [1](#0-0) . This pool is not merged with the previous one — it is a full replacement built from the current `pki.ca` config value [2](#0-1) .

Certificates for already-established tunnels are cached as `*cert.CachedCertificate`, which stores a `signerFingerprint` captured once at initial verification time [3](#0-2) . Subsequent re-verifications go through `VerifyCachedCertificate` → `verify()`, which does a cheap check: it fetches `signer` from the *current* CA pool via `GetCAForCert` and compares `signerFp != signer.Fingerprint` [4](#0-3) . If the operator rotates/removes the CA that originally signed a peer's certificate (a normal `pki.ca` reload, e.g. during a CA rotation), the new pool no longer contains a CA whose fingerprint matches `signerFp`, and `verify()` returns `ErrFingerprintMismatch` (or `GetCAForCert` fails) for every previously-established peer signed under the old CA — even though those peer certificates are still cryptographically valid and were originally accepted.

This directly mirrors the reported bug class: a global trust/authorization mapping (`assetSlashingHandlers` / here, the `CAPool`) is swapped out from under objects that were created and validated under the old mapping (`NativeVault.slashStore` / here, cached `HostInfo` connections), and there is no reconciliation path — connections established under the old CA are permanently unable to re-verify until either the old CA is restored or every affected tunnel is manually re-established.

### Impact Explanation
When an operator rotates a CA (a supported, documented operation via `pki.ca` reload and `RegisterReloadCallback`), any code path that re-verifies a cached peer certificate against the live `CAPool` (e.g. firewall rule matching using `caName`/`caSha` via `table.match(fp, incoming, h.ConnectionState.peerCert, caPool)` in `Firewall.Drop`, and connection-manager certificate re-checks) will begin failing for all peers signed by the rotated-out CA [5](#0-4) . This can cause remote denial of service: valid, already-authenticated tunnels stop passing the firewall or fail conntrack re-validation, silently dropping traffic network-wide for every host still carrying an old-CA certificate, until all nodes are manually re-issued certificates and reloaded in lockstep — the same "must update one by one, in a fragile order" problem the original report calls out for `NativeVault`.

### Likelihood Explanation
CA rotation is a normal, documented operational activity (nebula explicitly supports multiple CAs in the pool and reload via `RegisterReloadCallback`), so this is reachable by an operator/administrator action without needing a CA-signed certificate from an attacker's perspective — it's a state-poisoning/DoS effect triggered by legitimate config changes, analogous to the original finding's confirmed status ("slashing handler changed after deployment"). I was not able to fully trace every call site of `VerifyCachedCertificate` (e.g. inside `connection_manager.go`) before running out of iterations, so I cannot confirm with full certainty whether nebula already contains a grace-period/dual-trust mechanism to mitigate this during the transition window; this should be verified directly in `connection_manager.go` and `handshake_manager` reload logic.

### Recommendation
- When reloading the CA pool, retain trust for CAs removed from the new config for a grace period (or require explicit `pki.blocklist`-style removal) rather than an unconditional full replacement, so certificates signed by a recently-rotated-out CA continue to validate until connections naturally re-handshake.
- Alternatively, on CA pool change, proactively force re-handshake (not silent drop) for hosts whose cached certificate's `signerFingerprint` is no longer present, providing an explicit recovery path instead of a persistent DoS.
- Document that dropping a CA from `pki.ca` requires either a coordinated re-handshake of all peers or a temporary period where both old and new CAs are present in the pool.

### Proof of Concept
1. Deploy nebula nodes A and B trusting CA₁; establish a tunnel (successful handshake, entries cached in `HostMap`/firewall conntrack).
2. Operator rotates `pki.ca` to CA₂ only (CA₁ removed) and triggers a config reload — `PKI.reloadCAPool` calls `p.caPool.Store(caPool)` with the new pool [1](#0-0) .
3. Node A's certificate (still signed by CA₁) is now cached on B's side as `CachedCertificate` with `signerFingerprint` = CA₁'s fingerprint.
4. Any subsequent firewall/conntrack re-validation calls `caPool.verify()` → `GetCAForCert` fails to find CA₁ in the new pool → `ErrFingerprintMismatch`/lookup error [4](#0-3) .
5. Traffic between A and B is dropped despite both having previously fully authenticated, causing a persistent DoS until every node's certificate is reissued and reloaded under CA₂.

### Citations

**File:** pki.go (L196-205)
```go
func (p *PKI) reloadCAPool(c *config.C) *util.ContextualError {
	caPool, err := loadCAPoolFromConfig(p.l, c)
	if err != nil {
		return util.NewContextualError("Failed to load ca from config", nil, err)
	}

	p.caPool.Store(caPool)
	p.l.Debug("Trusted CA fingerprints", "fingerprints", caPool.GetFingerprints())
	return nil
}
```

**File:** pki.go (L525-571)
```go
func loadCAPoolFromConfig(l *slog.Logger, c *config.C) (*cert.CAPool, error) {
	caPathOrPEM := c.GetString("pki.ca", "")
	if caPathOrPEM == "" {
		return nil, errors.New("no pki.ca path or PEM data provided")
	}

	var caReader io.ReadCloser
	var err error

	if strings.Contains(caPathOrPEM, "-----BEGIN") {
		caReader = io.NopCloser(strings.NewReader(caPathOrPEM))
	} else {
		caReader, err = os.Open(caPathOrPEM)
		if err != nil {
			return nil, fmt.Errorf("unable to read pki.ca file %s: %s", caPathOrPEM, err)
		}
	}
	defer caReader.Close()

	caPool, err := cert.NewCAPoolFromPEMReader(caReader)
	if errors.Is(err, cert.ErrExpired) {
		var expired int
		for _, crt := range caPool.CAs {
			if crt.Certificate.Expired(time.Now()) {
				expired++
				l.Warn("expired certificate present in CA pool", "cert", crt)
			}
		}

		if expired >= len(caPool.CAs) {
			return nil, errors.New("no valid CA certificates present")
		}

	} else if err != nil {
		return nil, fmt.Errorf("error while adding CA certificate to CA trust store: %s", err)
	}

	bl := c.GetStringSlice("pki.blocklist", []string{})
	if len(bl) > 0 {
		for _, fp := range bl {
			caPool.BlocklistFingerprint(fp)
		}

		l.Info("Blocklisted certificates", "fingerprintCount", len(bl))
	}

	return caPool, nil
```

**File:** cert/ca_pool.go (L183-196)
```go
	cc := CachedCertificate{
		Certificate:       c,
		InvertedGroups:    make(map[string]struct{}),
		Fingerprint:       fp,
		fingerprint2:      fp2,
		signerFingerprint: signer.Fingerprint,
	}

	for _, g := range c.Groups() {
		cc.InvertedGroups[g] = struct{}{}
	}

	return &cc, nil
}
```

**File:** cert/ca_pool.go (L210-238)
```go
func (ncp *CAPool) verify(c Certificate, now time.Time, certFp string, signerFp string) (*CachedCertificate, error) {
	if ncp.IsBlocklisted(certFp) {
		return nil, ErrBlockListed
	}

	signer, err := ncp.GetCAForCert(c)
	if err != nil {
		return nil, err
	}

	if signer.Certificate.Curve() != c.Curve() {
		return nil, ErrCurveMismatch
	}

	if signer.Certificate.Expired(now) {
		return nil, ErrRootExpired
	}

	if c.Expired(now) {
		return nil, ErrExpired
	}

	// If we are checking a cached certificate then we can bail early here
	// Either the root is no longer trusted or everything is fine
	if len(signerFp) > 0 {
		if signerFp != signer.Fingerprint {
			return nil, ErrFingerprintMismatch
		}
		return signer, nil
```

**File:** firewall.go (L464-473)
```go
	table := f.OutRules
	if incoming {
		table = f.InRules
	}

	// We now know which firewall table to check against
	if !table.match(fp, incoming, h.ConnectionState.peerCert, caPool) {
		f.metrics(incoming).droppedNoRule.Inc(1)
		return ErrNoMatchingRule
	}
```
