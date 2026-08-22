### Title
Aggregate CA-pool-load guard masks an individually-expired/bad CA, letting a stale trust anchor remain active for certificate/firewall matching - (File: pki.go, cert/ca_pool.go)

### Summary
The reported HyperCore bug is a class of "aggregate guard bypass": a safety check is computed over the *sum/total* of several independently-sourced values instead of validating each value individually, so a bad/corrupted individual component is masked by the others and the guard silently passes. Nebula's CA-pool loading path exhibits the same structural pattern: `loadCAPoolFromConfig` only rejects the entire CA bundle if *all* CAs are found to be expired (`expired >= len(caPool.CAs)`), rather than individually excluding each expired CA from the trust pool.

### Finding Description
`cert.CAPool.AddCA` unconditionally inserts the parsed CA into `ncp.CAs[sum]` *before* checking expiration, and only returns an error afterward: [1](#0-0) 

`NewCAPoolFromPEMReader` / `loadCAPoolFromConfig` then aggregate these per-CA errors and only fail hard if the *count* of expired CAs equals the *total* count of CAs in the pool: [2](#0-1) 

This mirrors the reported bug precisely: instead of guarding each `delegatorSummary` component individually, the exchange-rate guard checked only the aggregate sum. Here, instead of individually excluding each expired CA from `ncp.CAs`, the loader checks only the aggregate count (`expired >= len(caPool.CAs)`) to decide whether to hard-fail. As long as at least one CA in the bundle is still valid, an expired CA silently remains resident in `ncp.CAs` and is fully usable by any code path that looks up a CA by fingerprint/name/sha without independently re-checking expiration — most notably firewall CA-based matching, which retrieves the signer via `caPool.GetCAForCert(c.Certificate)` and matches firewall rules against `s.Certificate.Name()` with no expiration check at that call site: [3](#0-2) 

While the primary handshake certificate-verification path (`CAPool.verify`) does separately re-check `signer.Certificate.Expired(now)`, the firewall CA-name/CA-sha matching path does not perform that check itself — it simply trusts whatever `ncp.CAs` returns. Because the aggregate "not all expired" guard at load time is the only gate preventing a stale/expired CA from persisting in the trust pool, a single stale (e.g., accidentally un-rotated or maliciously reintroduced) CA blended into a bundle with other valid CAs bypasses the intended per-CA expiration safety net.

### Impact Explanation
An expired CA that should have been excluded from the trust store remains active for firewall rule evaluation (`ca_name`/`ca_sha` rules), meaning traffic authorization decisions can be based on a certificate authority that operators believed was no longer trusted. This does not by itself forge a valid handshake (the handshake path still checks expiration), but it undermines the intended "CA lifecycle" security boundary and can cause firewall policy to remain keyed to a CA that should have been revoked/expired, a state-poisoning of the trust pool reachable purely through configuration ambiguity rather than intended per-item validation.

### Likelihood Explanation
Likelihood is moderate: it requires an operator-supplied CA bundle containing a mix of expired and valid CAs (a realistic operational scenario during CA rotation), and the class of impact is confined to firewall CA-matching rather than full authentication bypass, since `CAPool.verify` still independently checks expiration for certificate/handshake verification.

### Recommendation
Do not insert an expired CA into `ncp.CAs` in `AddCA` (or otherwise mark/exclude expired CAs individually), rather than only aggregating a count of expired CAs to decide whether to fail the whole load. Additionally, `FirewallCA.match`'s use of `caPool.GetCAForCert` should independently verify the signer's expiration before trusting CA-name/CA-sha based rule matches, mirroring the guard done in `CAPool.verify`.

### Proof of Concept
Not independently confirmed with a runnable exploit; this is inferred from static analysis of `AddCA`/`loadCAPoolFromConfig`/`FirewallCA.match`. To validate: build a CA bundle with one expired CA and one valid CA, load it via `pki.loadCAPoolFromConfig` (load succeeds because `expired < len(caPool.CAs)`), then issue a host certificate signed by the expired CA and add a firewall rule using `ca_name`/`ca_sha` matching that expired CA's name/fingerprint; observe that `FirewallCA.match` matches based on `caPool.GetCAForCert` without re-checking `Expired()`, even though the handshake path itself would separately reject the same certificate due to `ErrRootExpired` in `CAPool.verify`.

### Citations

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

**File:** pki.go (L525-572)
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
}
```

**File:** firewall.go (L746-767)
```go
func (fc *FirewallCA) match(p firewall.Packet, c *cert.CachedCertificate, caPool *cert.CAPool) bool {
	if fc == nil {
		return false
	}

	if fc.Any.match(p, c) {
		return true
	}

	if t, ok := fc.CAShas[c.Certificate.Issuer()]; ok {
		if t.match(p, c) {
			return true
		}
	}

	s, err := caPool.GetCAForCert(c.Certificate)
	if err != nil {
		return false
	}

	return fc.CANames[s.Certificate.Name()].match(p, c)
}
```
