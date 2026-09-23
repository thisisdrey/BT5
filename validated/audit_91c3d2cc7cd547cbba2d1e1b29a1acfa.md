No vulnerability found for this question.

The Strimzi CVE describes an application-level trust-store misconfiguration bug (Java code adding every certificate in a supplied CA chain as an independently trusted root/intermediate, rather than extracting and trusting only the terminal CA). This is Strimzi's own certificate-management logic, not a defect in Go's standard library.

Checking the closest Go analog in this repository, `crypto/x509.CertPool` and `Certificate.Verify` behave as documented: `CertPool.AppendCertsFromPEM`/`AddCert` intentionally add every certificate a caller supplies as an independent trust anchor (roots) or as an available intermediate — that's the documented, intended API contract [1](#0-0) . Chain building in `buildChains` requires an actual cryptographic signature link (`CheckSignatureFrom`) from child to parent before a certificate is accepted into a candidate chain, and the chain must terminate in `opts.Roots` [2](#0-1) [3](#0-2) . In `crypto/tls`, both client (`verifyServerCertificate`) and server (`processCertsFromClient`) handshake paths populate `Intermediates` from the peer-presented chain and call `Verify` against `RootCAs`/`ClientCAs` — they don't add peer-chain certificates as roots, so every non-leaf cert in the chain is validated by signature linkage to its actual parent, not trusted individually as a root [4](#0-3) [5](#0-4) .

This means Go's `crypto/tls`/`crypto/x509` don't exhibit the Strimzi bug class (accepting a leaf signed by *any* CA in an admin-configured trust chain instead of only the designated terminal CA) — that behavior in Strimzi stems from its own Java trust-manager construction, which has no counterpart defect here. There is no unauthenticated/attacker-reachable Go entry point where a chain of trusted-cert configuration is misinterpreted to trust intermediate CAs as independent roots beyond the documented `CertPool` semantics.

### Citations

**File:** src/crypto/x509/verify.go (L516-529)
```go
// Verify attempts to verify c by building one or more chains from c to a
// certificate in opts.Roots, using certificates in opts.Intermediates if
// needed. If successful, it returns one or more chains where the first
// element of the chain is c and the last element is from opts.Roots.
//
// If opts.Roots is nil, the platform verifier might be used, and
// verification details might differ from what is described below. If system
// roots are unavailable the returned error will be of type SystemRootsError.
//
// Name constraints in the intermediates will be applied to all names claimed
// in the chain, not just opts.DNSName. Thus it is invalid for a leaf to claim
// example.com if an intermediate doesn't permit it, even if example.com is not
// the name being validated. Note that DirectoryName constraints are not
// supported.
```

**File:** src/crypto/x509/verify.go (L602-610)
```go
	var candidateChains [][]*Certificate
	if opts.Roots.contains(c) {
		candidateChains = [][]*Certificate{{c}}
	} else {
		candidateChains, err = c.buildChains([]*Certificate{c}, nil, &opts)
		if err != nil {
			return nil, err
		}
	}
```

**File:** src/crypto/x509/verify.go (L737-757)
```go
	considerCandidate := func(certType int, candidate potentialParent) {
		if sigChecks == nil {
			sigChecks = new(int)
		}
		*sigChecks++
		if *sigChecks > maxChainSignatureChecks {
			err = errSignatureLimit
			return
		}

		if candidate.cert.PublicKey == nil || alreadyInChain(candidate.cert, currentChain) {
			return
		}

		if err := c.CheckSignatureFrom(candidate.cert); err != nil {
			if hintErr == nil {
				hintErr = err
				hintCert = candidate.cert
			}
			return
		}
```

**File:** src/crypto/tls/handshake_client.go (L1153-1168)
```go
	} else if !c.config.InsecureSkipVerify {
		opts := x509.VerifyOptions{
			Roots:         c.config.RootCAs,
			CurrentTime:   c.config.time(),
			DNSName:       c.config.ServerName,
			Intermediates: x509.NewCertPool(),
		}

		for _, cert := range certs[1:] {
			opts.Intermediates.AddCert(cert)
		}
		chains, err := certs[0].Verify(opts)
		if err != nil {
			c.sendAlert(alertBadCertificate)
			return &CertificateVerificationError{UnverifiedCertificates: certs, Err: err}
		}
```

**File:** src/crypto/tls/handshake_server.go (L970-992)
```go
	if c.config.ClientAuth >= VerifyClientCertIfGiven && len(certs) > 0 {
		opts := x509.VerifyOptions{
			Roots:         c.config.ClientCAs,
			CurrentTime:   c.config.time(),
			Intermediates: x509.NewCertPool(),
			KeyUsages:     []x509.ExtKeyUsage{x509.ExtKeyUsageClientAuth},
		}

		for _, cert := range certs[1:] {
			opts.Intermediates.AddCert(cert)
		}

		chains, err := certs[0].Verify(opts)
		if err != nil {
			if _, ok := errors.AsType[x509.UnknownAuthorityError](err); ok {
				c.sendAlert(alertUnknownCA)
			} else if errCertificateInvalid, ok := errors.AsType[x509.CertificateInvalidError](err); ok && errCertificateInvalid.Reason == x509.Expired {
				c.sendAlert(alertCertificateExpired)
			} else {
				c.sendAlert(alertBadCertificate)
			}
			return &CertificateVerificationError{UnverifiedCertificates: certs, Err: err}
		}
```
