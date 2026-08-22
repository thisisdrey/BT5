### Title
Unauthenticated Certificate-Version Downgrade During Handshake Negotiation - (File: `handshake/machine.go`)

### Summary
The external report flags inconsistent use of different version directives across a project as a medium-severity risk because mixed versions can be exploited to force weaker code paths. In this Go codebase, an analogous condition exists in the certificate/handshake layer: Nebula simultaneously supports `cert.Version1` and `cert.Version2` certificate formats, and `Machine.validateCert` silently switches the locally-used certificate version to match whatever version the remote peer presents, before certificate verification constraints for that path are enforced beyond CA-pool checks.

### Finding Description
`newCertStateFromConfig`/`newCertState` in `pki.go` allow a node to be configured with both a V1 and a V2 certificate simultaneously (a supported migration scenario), each with its own credential. [1](#0-0) [2](#0-1) 

During the handshake, `Machine.validateCert` reconstructs the peer certificate using the version number the peer supplied on the wire (`payload.CertVersion`), and if that version differs from the locally active `m.myVersion`, and a local credential for that version exists, the machine unconditionally switches to it: [3](#0-2) 

This switch happens purely based on an attacker-controlled/peer-controlled version field before the certificate has passed full CA-pool verification (`m.verifier(rc)` is called afterward), and it directly determines which certificate/credential this node will present in its own next outgoing handshake message (`marshalOutgoing` uses `m.getCred(m.myVersion)`). [4](#0-3) 

Because V1 and V2 certificates have materially different validation rules — e.g. V1 supports only IPv4 and ignores network sort/uniqueness ("v1 doesn't bother with sort order or uniqueness"), while V2 enforces IPv4/IPv6 correctness, sorted/unique network lists, and stricter unsafe-network/IP-family checks — an operator running mixed V1/V2 certificates for migration purposes can be forced by a remote (still CA-trusted) peer to negotiate down to the weaker V1 certificate path on every handshake, simply by presenting a valid V1 cert instead of V2, even when V2 is preferred/configured as the initiating version. [5](#0-4) [6](#0-5) 

This exact scenario is exercised (and treated as expected behavior, not blocked) by the project's own e2e tests `TestCertUpgrade`/`TestCertDowngrade`/`TestCertMismatchCorrection`, which show that whichever version a peer presents causes the other side to converge to that version. [7](#0-6) 

### Impact Explanation
While the certificate itself must still pass full CA-pool verification (fingerprint blocklist, curve match, expiry, signature, CA constraints) via `CAPool.verify`, the choice of *which* certificate format/validation rules are applied is driven entirely by peer-supplied data rather than local policy. [8](#0-7) 
In a mixed-version deployment (a supported, documented transition state per `pki.initiating_version`), this allows an attacker who controls or has compromised a peer's V1 identity to force downgrade of a target's outgoing certificate presentation and validation logic to the weaker V1 code path network-wide, undermining the intended security benefits of migrating to V2 (e.g., stricter address-family and network-list validation). This does not constitute a full authentication bypass since a trusted CA signature is still required, but it is a real cross-version consistency weakness matching the "different directive versions used inconsistently" bug class from the report.

### Likelihood Explanation
Exploitability requires the attacker to control or possess a certificate for a version other than the one currently negotiated by the target — a condition automatically satisfied whenever an operator maintains dual V1/V2 certificates during a migration window (explicitly supported by `pki.cert`/`pki.initiating_version`). No malicious-CA or invalid-certificate assumption is required, and the negotiation happens automatically and silently on every handshake without any log/warning surfaced to the operator, making unintended silent downgrade likely in mixed-fleet deployments.

### Recommendation
Do not allow the locally used certificate version to be switched based on unauthenticated peer input. Instead: (1) determine the version to use strictly from local configuration (`pki.initiating_version`), and only accept the peer certificate if it is presented in the version the local policy allows/requires, or (2) if flexible negotiation is genuinely desired, cache the CA-verified certificate's version and log/audit version transitions, and never allow "downgrade" to a lower version once a higher version has been used with a peer, mirroring TLS anti-downgrade defenses.

### Proof of Concept
1. Configure Node A with both a V1 and V2 certificate (`pki.cert` containing both PEM blocks) and `pki.initiating_version: 2`.
2. Configure Node B (attacker-controlled or compromised) with only a valid, CA-trusted V1 certificate.
3. Node B initiates or responds to a handshake with Node A, presenting its V1 certificate (`payload.CertVersion = 1`).
4. In `Machine.validateCert`, Node A finds `rc.Version() (=1) != m.myVersion (=2)` and finds a local V1 credential, so it sets `m.myVersion = cert.Version1`.
5. Node A's subsequent handshake payload now carries its V1 certificate (`marshalOutgoing` at `handshake/machine.go:406-414`), meaning Node A is now operating under V1's weaker certificate-validation rules for this and potentially future negotiated sessions, purely because the peer chose to present a V1 certificate. This mirrors the exact test flow in `TestCertDowngrade`/`TestCertMismatchCorrection`. [9](#0-8)

### Citations

**File:** pki.go (L320-342)
```go

	var crt, v1, v2 cert.Certificate
	for {
		// Load the certificate
		crt, rawCert, err = loadCertificate(rawCert)
		if err != nil {
			return nil, err
		}

		switch crt.Version() {
		case cert.Version1:
			if v1 != nil {
				return nil, fmt.Errorf("v1 certificate already found in pki.cert")
			}
			v1 = crt
		case cert.Version2:
			if v2 != nil {
				return nil, fmt.Errorf("v2 certificate already found in pki.cert")
			}
			v2 = crt
		default:
			return nil, fmt.Errorf("unknown certificate version %v", crt.Version())
		}
```

**File:** pki.go (L403-451)
```go
	if v1 != nil {
		if pkcs11backed {
			//NOTE: We do not currently have a method to verify a public private key pair when the private key is in an hsm
		} else {
			if err := v1.VerifyPrivateKey(privateKeyCurve, privateKey); err != nil {
				return nil, fmt.Errorf("private key is not a pair with public key in nebula cert")
			}
		}

		v1hs, err := v1.MarshalForHandshakes()
		if err != nil {
			return nil, fmt.Errorf("error marshalling v1 certificate for handshake: %w", err)
		}
		ncs, err := newCipherSuite(v1.Curve(), pkcs11backed, cipher)
		if err != nil {
			return nil, err
		}
		cs.v1Cert = v1
		cs.v1Credential = handshake.NewCredential(v1, v1hs, privateKey, ncs)

		if cs.initiatingVersion == 0 {
			cs.initiatingVersion = cert.Version1
		}
	}

	if v2 != nil {
		if pkcs11backed {
			//NOTE: We do not currently have a method to verify a public private key pair when the private key is in an hsm
		} else {
			if err := v2.VerifyPrivateKey(privateKeyCurve, privateKey); err != nil {
				return nil, fmt.Errorf("private key is not a pair with public key in nebula cert")
			}
		}

		v2hs, err := v2.MarshalForHandshakes()
		if err != nil {
			return nil, fmt.Errorf("error marshalling v2 certificate for handshake: %w", err)
		}
		ncs, err := newCipherSuite(v2.Curve(), pkcs11backed, cipher)
		if err != nil {
			return nil, err
		}
		cs.v2Cert = v2
		cs.v2Credential = handshake.NewCredential(v2, v2hs, privateKey, ncs)

		if cs.initiatingVersion == 0 {
			cs.initiatingVersion = cert.Version2
		}
	}
```

**File:** handshake/machine.go (L342-369)
```go
func (m *Machine) validateCert(payload Payload) error {
	cred := m.getCred(m.myVersion)
	if cred == nil {
		m.failed = true
		return fmt.Errorf("%w: %v", ErrNoCredential, m.myVersion)
	}
	rc, err := cert.Recombine(
		cert.Version(payload.CertVersion),
		payload.Cert,
		m.hs.PeerStatic(),
		cred.Cert.Curve(),
	)
	if err != nil {
		m.failed = true
		return fmt.Errorf("recombine cert: %w", err)
	}

	if !bytes.Equal(rc.PublicKey(), m.hs.PeerStatic()) {
		m.failed = true
		return ErrPublicKeyMismatch
	}

	// Version negotiation, if the peer sent a different version and we have it, switch
	if rc.Version() != m.myVersion {
		if m.getCred(rc.Version()) != nil {
			m.myVersion = rc.Version()
		}
	}
```

**File:** handshake/machine.go (L406-414)
```go
	if flags.expectsCert {
		cred := m.getCred(m.myVersion)
		if cred == nil {
			return nil, fmt.Errorf("%w: %v", ErrNoCredential, m.myVersion)
		}
		p.Cert = cred.Bytes
		p.CertVersion = uint32(cred.Cert.Version())
		m.result.MyCert = cred.Cert
	}
```

**File:** cert/cert_v1.go (L332-381)
```go
func (c *certificateV1) validate() error {
	// Empty names are allowed

	if len(c.details.publicKey) == 0 {
		return ErrInvalidPublicKey
	}

	// Original v1 rules allowed multiple networks to be present but ignored all but the first one.
	// Continue to allow this behavior
	if !c.details.isCA && len(c.details.networks) == 0 {
		return NewErrInvalidCertificateProperties("non-CA certificates must contain exactly one network")
	}

	for _, network := range c.details.networks {
		if !network.IsValid() || !network.Addr().IsValid() {
			return NewErrInvalidCertificateProperties("invalid network: %s", network)
		}

		if network.Addr().Is6() {
			return NewErrInvalidCertificateProperties("certificate may not contain IPv6 networks: %v", network)
		}

		if network.Addr().IsUnspecified() {
			return NewErrInvalidCertificateProperties("non-CA certificates must not use the zero address as a network: %s", network)
		}

		if network.Addr().Zone() != "" {
			return NewErrInvalidCertificateProperties("networks may not contain zones: %s", network)
		}
	}

	for _, network := range c.details.unsafeNetworks {
		if !network.IsValid() || !network.Addr().IsValid() {
			return NewErrInvalidCertificateProperties("invalid unsafe network: %s", network)
		}

		if network.Addr().Is6() {
			return NewErrInvalidCertificateProperties("certificate may not contain IPv6 unsafe networks: %v", network)
		}

		if network.Addr().Zone() != "" {
			return NewErrInvalidCertificateProperties("unsafe networks may not contain zones: %s", network)
		}
	}

	// v1 doesn't bother with sort order or uniqueness of networks or unsafe networks.
	// We can't modify the unmarshalled data because verification requires re-marshalling and a re-ordered
	// unsafe networks would result in a different signature.

	return nil
```

**File:** cert/cert_v2.go (L391-460)
```go
func (c *certificateV2) validate() error {
	// Empty names are allowed

	if len(c.publicKey) == 0 {
		return ErrInvalidPublicKey
	}

	if !c.details.isCA && len(c.details.networks) == 0 {
		return NewErrInvalidCertificateProperties("non-CA certificate must contain at least 1 network")
	}

	hasV4Networks := false
	hasV6Networks := false
	for _, network := range c.details.networks {
		if !network.IsValid() || !network.Addr().IsValid() {
			return NewErrInvalidCertificateProperties("invalid network: %s", network)
		}

		if network.Addr().IsUnspecified() {
			return NewErrInvalidCertificateProperties("non-CA certificates must not use the zero address as a network: %s", network)
		}

		if network.Addr().Zone() != "" {
			return NewErrInvalidCertificateProperties("networks may not contain zones: %s", network)
		}

		if network.Addr().Is4In6() {
			return NewErrInvalidCertificateProperties("4in6 networks are not allowed: %s", network)
		}

		hasV4Networks = hasV4Networks || network.Addr().Is4()
		hasV6Networks = hasV6Networks || network.Addr().Is6()
	}

	slices.SortFunc(c.details.networks, comparePrefix)
	err := findDuplicatePrefix(c.details.networks)
	if err != nil {
		return err
	}

	for _, network := range c.details.unsafeNetworks {
		if !network.IsValid() || !network.Addr().IsValid() {
			return NewErrInvalidCertificateProperties("invalid unsafe network: %s", network)
		}

		if network.Addr().Zone() != "" {
			return NewErrInvalidCertificateProperties("unsafe networks may not contain zones: %s", network)
		}

		if !c.details.isCA {
			if network.Addr().Is6() {
				if !hasV6Networks {
					return NewErrInvalidCertificateProperties("IPv6 unsafe networks require an IPv6 address assignment: %s", network)
				}
			} else if network.Addr().Is4() {
				if !hasV4Networks {
					return NewErrInvalidCertificateProperties("IPv4 unsafe networks require an IPv4 address assignment: %s", network)
				}
			}
		}
	}

	slices.SortFunc(c.details.unsafeNetworks, comparePrefix)
	err = findDuplicatePrefix(c.details.unsafeNetworks)
	if err != nil {
		return err
	}

	return nil
}
```

**File:** e2e/tunnels_test.go (L162-260)
```go
func TestCertDowngrade(t *testing.T) {
	t.Parallel()
	// The goal of this test is to ensure the shortest inactivity timeout will close the tunnel on both sides
	// under ideal conditions
	ca, _, caKey, _ := cert_test.NewTestCaCert(cert.Version1, cert.Curve_CURVE25519, time.Now(), time.Now().Add(10*time.Minute), nil, nil, []string{})
	caB, err := ca.MarshalPEM()
	if err != nil {
		panic(err)
	}
	ca2, _, caKey2, _ := cert_test.NewTestCaCert(cert.Version2, cert.Curve_CURVE25519, time.Now(), time.Now().Add(10*time.Minute), nil, nil, []string{})

	ca2B, err := ca2.MarshalPEM()
	if err != nil {
		panic(err)
	}
	caStr := fmt.Sprintf("%s\n%s", caB, ca2B)

	myCert, _, myPrivKey, myCertPem := cert_test.NewTestCert(cert.Version1, cert.Curve_CURVE25519, ca, caKey, "me", time.Now(), time.Now().Add(5*time.Minute), []netip.Prefix{netip.MustParsePrefix("10.128.0.1/24")}, nil, []string{})
	myCert2, _ := cert_test.NewTestCertDifferentVersion(myCert, cert.Version2, ca2, caKey2)

	theirCert, _, theirPrivKey, _ := cert_test.NewTestCert(cert.Version1, cert.Curve_CURVE25519, ca, caKey, "them", time.Now(), time.Now().Add(5*time.Minute), []netip.Prefix{netip.MustParsePrefix("10.128.0.2/24")}, nil, []string{})
	theirCert2, _ := cert_test.NewTestCertDifferentVersion(theirCert, cert.Version2, ca2, caKey2)

	myControl, myVpnIpNet, myUdpAddr, myC := newServer([]cert.Certificate{ca, ca2}, []cert.Certificate{myCert2}, myPrivKey, m{})
	theirControl, theirVpnIpNet, theirUdpAddr, _ := newServer([]cert.Certificate{ca, ca2}, []cert.Certificate{theirCert, theirCert2}, theirPrivKey, m{})

	// Share our underlay information
	myControl.InjectLightHouseAddr(theirVpnIpNet[0].Addr(), theirUdpAddr)
	theirControl.InjectLightHouseAddr(myVpnIpNet[0].Addr(), myUdpAddr)

	// Start the servers
	myControl.Start()
	theirControl.Start()

	r := router.NewR(t, myControl, theirControl)
	defer r.RenderFlow()

	r.Log("Assert the tunnel between me and them works")
	//assertTunnel(t, theirVpnIpNet[0].Addr(), myVpnIpNet[0].Addr(), theirControl, myControl, r)
	//r.Log("yay")
	assertTunnel(t, myVpnIpNet[0].Addr(), theirVpnIpNet[0].Addr(), myControl, theirControl, r)
	r.Log("yay")
	//todo ???
	time.Sleep(1 * time.Second)
	r.FlushAll()

	mc := m{
		"pki": m{
			"ca":   caStr,
			"cert": string(myCertPem),
			"key":  string(myPrivKey),
		},
		"firewall": myC.Settings["firewall"],
		"listen":   myC.Settings["listen"],
		"logging":  myC.Settings["logging"],
		"timers":   myC.Settings["timers"],
	}

	cb, err := yaml.Marshal(mc)
	if err != nil {
		panic(err)
	}

	r.Logf("reload new v1-only config")
	err = myC.ReloadConfigString(string(cb))
	assert.NoError(t, err)
	r.Log("yay, spin until their sees it")
	waitStart := time.Now()
	for {
		assertTunnel(t, myVpnIpNet[0].Addr(), theirVpnIpNet[0].Addr(), myControl, theirControl, r)
		c := theirControl.GetHostInfoByVpnAddr(myVpnIpNet[0].Addr(), false)
		c2 := myControl.GetHostInfoByVpnAddr(theirVpnIpNet[0].Addr(), false)
		if c == nil || c2 == nil {
			r.Log("nil")
		} else {
			version := c.Cert.Version()
			theirVersion := c2.Cert.Version()
			r.Logf("version %d,%d", version, theirVersion)
			if version == cert.Version1 {
				break
			}
		}
		since := time.Since(waitStart)
		if since > time.Second*5 {
			r.Log("it is unusual that the cert is not new yet, but not a failure yet")
		}
		if since > time.Second*10 {
			r.Log("wtf")
			t.Fatal("Cert should be new by now")
		}
		time.Sleep(time.Second)
	}

	r.RenderHostmaps("Final hostmaps", myControl, theirControl)

	myControl.Stop()
	theirControl.Stop()
}

```

**File:** e2e/tunnels_test.go (L261-327)
```go
func TestCertMismatchCorrection(t *testing.T) {
	t.Parallel()
	// The goal of this test is to ensure the shortest inactivity timeout will close the tunnel on both sides
	// under ideal conditions
	ca, _, caKey, _ := cert_test.NewTestCaCert(cert.Version1, cert.Curve_CURVE25519, time.Now(), time.Now().Add(10*time.Minute), nil, nil, []string{})
	ca2, _, caKey2, _ := cert_test.NewTestCaCert(cert.Version2, cert.Curve_CURVE25519, time.Now(), time.Now().Add(10*time.Minute), nil, nil, []string{})

	myCert, _, myPrivKey, _ := cert_test.NewTestCert(cert.Version1, cert.Curve_CURVE25519, ca, caKey, "me", time.Now(), time.Now().Add(5*time.Minute), []netip.Prefix{netip.MustParsePrefix("10.128.0.1/24")}, nil, []string{})
	myCert2, _ := cert_test.NewTestCertDifferentVersion(myCert, cert.Version2, ca2, caKey2)

	theirCert, _, theirPrivKey, _ := cert_test.NewTestCert(cert.Version1, cert.Curve_CURVE25519, ca, caKey, "them", time.Now(), time.Now().Add(5*time.Minute), []netip.Prefix{netip.MustParsePrefix("10.128.0.2/24")}, nil, []string{})
	theirCert2, _ := cert_test.NewTestCertDifferentVersion(theirCert, cert.Version2, ca2, caKey2)

	myControl, myVpnIpNet, myUdpAddr, _ := newServer([]cert.Certificate{ca, ca2}, []cert.Certificate{myCert2}, myPrivKey, m{})
	theirControl, theirVpnIpNet, theirUdpAddr, _ := newServer([]cert.Certificate{ca, ca2}, []cert.Certificate{theirCert, theirCert2}, theirPrivKey, m{})

	// Share our underlay information
	myControl.InjectLightHouseAddr(theirVpnIpNet[0].Addr(), theirUdpAddr)
	theirControl.InjectLightHouseAddr(myVpnIpNet[0].Addr(), myUdpAddr)

	// Start the servers
	myControl.Start()
	theirControl.Start()

	r := router.NewR(t, myControl, theirControl)
	defer r.RenderFlow()

	r.Log("Assert the tunnel between me and them works")
	//assertTunnel(t, theirVpnIpNet[0].Addr(), myVpnIpNet[0].Addr(), theirControl, myControl, r)
	//r.Log("yay")
	assertTunnel(t, myVpnIpNet[0].Addr(), theirVpnIpNet[0].Addr(), myControl, theirControl, r)
	r.Log("yay")
	//todo ???
	time.Sleep(1 * time.Second)
	r.FlushAll()

	waitStart := time.Now()
	for {
		assertTunnel(t, myVpnIpNet[0].Addr(), theirVpnIpNet[0].Addr(), myControl, theirControl, r)
		c := theirControl.GetHostInfoByVpnAddr(myVpnIpNet[0].Addr(), false)
		c2 := myControl.GetHostInfoByVpnAddr(theirVpnIpNet[0].Addr(), false)
		if c == nil || c2 == nil {
			r.Log("nil")
		} else {
			version := c.Cert.Version()
			theirVersion := c2.Cert.Version()
			r.Logf("version %d,%d", version, theirVersion)
			if version == theirVersion {
				break
			}
		}
		since := time.Since(waitStart)
		if since > time.Second*5 {
			r.Log("wtf")
		}
		if since > time.Second*10 {
			r.Log("wtf")
			t.Fatal("Cert should be new by now")
		}
		time.Sleep(time.Second)
	}

	r.RenderHostmaps("Final hostmaps", myControl, theirControl)

	myControl.Stop()
	theirControl.Stop()
}
```

**File:** cert/ca_pool.go (L210-250)
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
}
```
