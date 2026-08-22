### Title
Immediate CA pool trust update on config reload with no staged/pending delay - (File: `pki.go`)

### Summary
An operator with control over the running node's configuration (equivalent role to the "LP admin" in the original report) can update the `pki.ca` setting and trigger a reload, which atomically swaps the entire CA trust pool with zero validation period, staging, or delay. Any certificate signed by a newly-added (potentially malicious or compromised) CA is instantly and unconditionally trusted for handshake authentication network-wide, mirroring the "instant rug pull via unvetted core registration" bug class described in the external report.

### Finding Description
The `PKI.reload` function is invoked on every config reload (including SIGHUP) and calls `reloadCAPool`, which parses the `pki.ca` config value into a brand new `cert.CAPool` and immediately stores it via `p.caPool.Store(caPool)`, replacing the previous trust store with no intermediate/pending state: [1](#0-0) 

This pool is loaded via `loadCAPoolFromConfig`, which reads `pki.ca` directly from config (path or inline PEM) and adds any well-formed, self-signed CA certificate straight into the trust store with `AddCA`/`NewCAPoolFromPEMReader`: [2](#0-1) [3](#0-2) 

Once stored, `GetCAPool()` is used directly by the handshake manager's certificate verifier to authenticate every peer's certificate: [4](#0-3) 

Because `VerifyCertificate`/`verify` trust any CA present in `ncp.CAs` without any grace period, staging, quorum, or delayed-activation mechanism, the moment a new CA is present in `pki.ca` and a reload occurs, certificates signed by that CA are accepted for handshakes immediately: [5](#0-4) 

This is structurally identical to the reported bug class: a privileged actor (config-controlling admin) can introduce a new trusted authority ("core") that is granted full trust the instant it is registered, with no pending/delay window during which the change could be reviewed, reverted, or detected before damage occurs.

### Impact Explanation
If an admin config channel is compromised or a malicious/rogue operator adds an attacker-controlled CA to `pki.ca`, the attacker can immediately mint certificates that are fully trusted by every node that reloads that config — enabling authentication bypass (impersonating any host/group), which leads to unauthorized decrypted tunnel establishment, traffic interception, and firewall rule bypass (since firewall rules are enforced based on certificate groups/names). This is a full authentication-trust compromise with no remediation window, matching the "High impact" rating of the rug-pull report.

### Likelihood Explanation
Low, matching the source report: it requires a malicious or compromised entity with the ability to modify and reload the node's config (`pki.ca`), which is a privileged administrative action analogous to the compromised/malicious `LP` admin in the original report. It does not require an attacker to hold a valid certificate.

### Recommendation
Introduce a staged/pending-trust mechanism for new CA additions to the pool, analogous to the report's recommendation of a time delay before a new `core` becomes active in the `LP`:
- Support marking newly introduced CAs as "pending" for a configurable delay period after being observed in a reload, only promoting them to active trust after the delay elapses (and ideally after being logged/alerted for operator review).
- Alternatively, require an explicit two-phase confirmation (e.g., add-then-activate) for new CA fingerprints rather than a single atomic `ReloadConfig` swap.
- At minimum, emit high-severity alerts/audit logs whenever the CA pool's fingerprint set changes on reload so unexpected/unauthorized CA additions are detectable before large-scale abuse.

### Proof of Concept
1. Attacker gains ability to modify the running node's config file (e.g., via compromised deployment pipeline or insider access) and appends a self-generated malicious CA PEM to `pki.ca`.
2. Attacker triggers `SIGHUP`/config reload; `PKI.reload` → `reloadCAPool` → `loadCAPoolFromConfig` parses and calls `cert.NewCAPoolFromPEMReader`, adding the malicious CA to `CAPool.CAs` immediately, then `p.caPool.Store(caPool)` swaps the active pool in one atomic operation (see `pki.go:196-205`).
3. Attacker signs a certificate with the malicious CA for any identity/group and initiates a handshake with the target node.
4. `HandshakeManager.certVerifier` calls `hm.f.pki.GetCAPool().VerifyCertificate(...)`, which succeeds because the malicious CA is now present in the trust pool, granting the attacker's certificate full trust with no delay or review step (`cert/ca_pool.go:157-196`, `handshake_manager.go:1161-1166`).

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

**File:** pki.go (L525-560)
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
```

**File:** cert/ca_pool.go (L100-132)
```go
// AddCA verifies a Nebula CA certificate and adds it to the pool.
func (ncp *CAPool) AddCA(c Certificate) error {
	if !c.IsCA() {
		return fmt.Errorf("%s: %w", c.Name(), ErrNotCA)
	}

	if !c.CheckSignature(c.PublicKey()) {
		return fmt.Errorf("%s: %w", c.Name(), ErrNotSelfSigned)
	}

	sum, err := c.Fingerprint()
	if err != nil {
		return fmt.Errorf("could not calculate fingerprint for provided CA; error: %w; %s", err, c.Name())
	}

	cc := &CachedCertificate{
		Certificate:    c,
		Fingerprint:    sum,
		InvertedGroups: make(map[string]struct{}),
	}

	for _, g := range c.Groups() {
		cc.InvertedGroups[g] = struct{}{}
	}

	ncp.CAs[sum] = cc

	if c.Expired(time.Now()) {
		return fmt.Errorf("%s: %w", c.Name(), ErrExpired)
	}

	return nil
}
```

**File:** cert/ca_pool.go (L210-249)
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
	}
	if !c.CheckSignature(signer.Certificate.PublicKey()) {
		return nil, ErrSignatureMismatch
	}

	err = CheckCAConstraints(signer.Certificate, c)
	if err != nil {
		return nil, err
	}

	return signer, nil
```

**File:** handshake_manager.go (L1161-1166)
```go
// certVerifier returns a CertVerifier that validates certs against the current CA pool.
func (hm *HandshakeManager) certVerifier() handshake.CertVerifier {
	return func(c cert.Certificate) (*cert.CachedCertificate, error) {
		return hm.f.pki.GetCAPool().VerifyCertificate(time.Now(), c)
	}
}
```
