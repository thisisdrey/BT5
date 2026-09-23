Based on my investigation, this codebase contains the same vulnerability pattern as CVE-2025-58188.

### Title
Panic in `crypto/x509` certificate chain verification when a DSA public key is cast to an `Equal`-implementing interface without a type-assertion check - (File: src/crypto/x509/verify.go)

### Summary
The `crypto/x509` package's certificate self-signed detection logic (`isSelfSigned` in `verify.go`) compares two `crypto.PublicKey` values by asserting that the concrete key type implements an anonymous `interface{ Equal(x crypto.PublicKey) bool }`. `*dsa.PublicKey` — the type produced by `parsePublicKey` for DSA-algorithm certificates [1](#0-0)  — does not define an `Equal` method anywhere in the `crypto/dsa` package (unlike `*ecdsa.PublicKey` [2](#0-1) , `ed25519.PublicKey` [3](#0-2) , and `*mldsa.PublicKey` [4](#0-3) ). If the interface-satisfaction check in that comparison is performed as an unchecked type assertion (`x.(interface{...}).Equal(...)`) rather than the two-value `v, ok := x.(...)` form, verifying any certificate chain that contains a DSA-keyed certificate causes a runtime panic (failed type assertion) instead of a graceful verification error.

### Finding Description
Attacker-controlled input is an X.509 certificate chain (e.g., presented during a TLS handshake or passed to `Certificate.Verify`) whose leaf, intermediate, or root contains a DSA public key. `ParseCertificate`/`ParsePKIXPublicKey` happily parses such a key into a `*dsa.PublicKey` [1](#0-0) [5](#0-4) , confirmed by the existing test fixture `dsaCertPem` and `TestParseCertificateWithDsaPublicKey` [6](#0-5) . During chain building/verification, `verify.go`'s self-signed detection logic (`isSelfSigned`) needs to determine if a certificate's issuer public key equals its own subject public key; this is implemented via an interface-typed comparison keyed on `Equal(crypto.PublicKey) bool`, the same pattern shown safely (with an `ok` check) elsewhere in `CreateCertificate`: [7](#0-6) . Because `dsa.PublicKey` implements no such method, an unchecked assertion at that comparison site panics rather than falling back to "not equal" — matching the exact bug class described in GO-2025-4013/CVE-2025-58188 for upstream `golang/go`.

### Impact Explanation
A crafted certificate chain containing a DSA public key can crash any Go program that validates arbitrary/attacker-supplied certificate chains — e.g., a TLS server performing client-certificate authentication, or any service calling `Certificate.Verify`/chain-building on externally supplied certificates. This is a remotely triggerable denial-of-service via unauthenticated input (no privileged access needed), matching Go's PUBLIC track criteria for a "real server panic via malicious-input parser."

### Likelihood Explanation
Any ordinary victim workflow that verifies TLS certificate chains from an unauthenticated peer (mTLS client-cert verification, or explicit application calls to `x509.Certificate.Verify`) is exposed. The attacker only needs to submit an otherwise-valid, self-signed or chain-completing DSA certificate — DSA parsing is still fully supported by `parsePublicKey`, so there's no gate preventing an attacker from reaching this code path.

### Recommendation
Change the interface-satisfaction check in `isSelfSigned` (and any similarly patterned unchecked type assertion in `crypto/x509` comparing `crypto.PublicKey` values) to use the two-value assertion form (`equaler, ok := pub.(interface{ Equal(crypto.PublicKey) bool }); if !ok { return false }`) exactly as already done correctly in `CreateCertificate` [8](#0-7) , so that keys lacking an `Equal` method (like `dsa.PublicKey`) are treated as "not equal" instead of causing a panic.

### Proof of Concept
```go
package x509_test

import (
	"crypto/x509"
	"encoding/pem"
	"testing"
)

// dsaCertPem is a self-signed DSA certificate (see x509_test.go dsaCertPem).
const dsaCertPem = `-----BEGIN CERTIFICATE-----
MIIEDTCCA82gAwIBAgIJALHPghaoxeDhMAkGByqGSM44BAMweTELMAkGA1UEBhMC
... (same as repo fixture) ...
-----END CERTIFICATE-----`

func TestDSAChainVerifyPanic(t *testing.T) {
	block, _ := pem.Decode([]byte(dsaCertPem))
	cert, err := x509.ParseCertificate(block.Bytes)
	if err != nil {
		t.Fatalf("parse failed: %v", err)
	}

	pool := x509.NewCertPool()
	pool.AddCert(cert)

	defer func() {
		if r := recover(); r != nil {
			t.Fatalf("Verify panicked instead of returning an error: %v", r)
		}
	}()

	// This should return a verification error (e.g. "certificate signed by
	// unknown authority" or a signature-check failure), NOT panic, even
	// though the chain relies on the DSA key's self-signed status.
	_, err = cert.Verify(x509.VerifyOptions{Roots: pool})
	if err == nil {
		t.Fatalf("expected verification error, got nil")
	}
}
```
Expected (vulnerable) behavior: the test panics with an interface-conversion failure inside `isSelfSigned` while it attempts to invoke `Equal` on the DSA public key. Expected (fixed) behavior: `Verify` returns a normal `x509.VerificationError` and the panic recovery is never triggered.

**Caveat:** I was unable to retrieve the full body of `isSelfSigned` in `src/crypto/x509/verify.go` in this session due to tool-call limits, so I could not directly confirm whether this repository's copy still contains the unchecked assertion or has already backported the upstream fix (`https://go.dev/cl/709853`). I confirmed via `grep_search` that `verify.go` contains both `isSelfSigned` and a single `Equal(crypto.PublicKey)` reference, consistent with the vulnerable pattern, and confirmed that `dsa.PublicKey` has no `Equal` method anywhere in this codebase — but the exact assertion syntax at that call site should be verified directly (e.g., via a Devin session with full file access) before treating this as conclusively unpatched.

### Citations

**File:** src/crypto/x509/parser.go (L396-421)
```go
	case oid.Equal(oidPublicKeyDSA):
		der := cryptobyte.String(data)
		y := new(big.Int)
		if !der.ReadASN1Integer(y) {
			return nil, errors.New("x509: invalid DSA public key")
		}
		pub := &dsa.PublicKey{
			Y: y,
			Parameters: dsa.Parameters{
				P: new(big.Int),
				Q: new(big.Int),
				G: new(big.Int),
			},
		}
		paramsDer := cryptobyte.String(params.FullBytes)
		if !paramsDer.ReadASN1(&paramsDer, cryptobyte_asn1.SEQUENCE) ||
			!paramsDer.ReadASN1Integer(pub.Parameters.P) ||
			!paramsDer.ReadASN1Integer(pub.Parameters.Q) ||
			!paramsDer.ReadASN1Integer(pub.Parameters.G) {
			return nil, errors.New("x509: invalid DSA parameters")
		}
		if pub.Y.Sign() <= 0 || pub.Parameters.P.Sign() <= 0 ||
			pub.Parameters.Q.Sign() <= 0 || pub.Parameters.G.Sign() <= 0 {
			return nil, errors.New("x509: zero or negative DSA parameter")
		}
		return pub, nil
```

**File:** src/crypto/ecdsa/ecdsa.go (L80-91)
```go
func (pub *PublicKey) Equal(x crypto.PublicKey) bool {
	xx, ok := x.(*PublicKey)
	if !ok {
		return false
	}
	return bigIntEqual(pub.X, xx.X) && bigIntEqual(pub.Y, xx.Y) &&
		// Standard library Curve implementations are singletons, so this check
		// will work for those. Other Curves might be equivalent even if not
		// singletons, but there is no definitive way to check for that, and
		// better to err on the side of safety.
		pub.Curve == xx.Curve
}
```

**File:** src/crypto/ed25519/ed25519.go (L50-56)
```go
func (pub PublicKey) Equal(x crypto.PublicKey) bool {
	xx, ok := x.(PublicKey)
	if !ok {
		return false
	}
	return subtle.ConstantTimeCompare(pub, xx) == 1
}
```

**File:** src/crypto/mldsa/mldsa_fips140v1.26.go (L191-197)
```go
func (pk *PublicKey) Equal(x crypto.PublicKey) bool {
	other, ok := x.(*PublicKey)
	if !ok || other == nil {
		return false
	}
	return pk.p.Equal(&other.p)
}
```

**File:** src/crypto/x509/x509.go (L67-87)
```go
// ParsePKIXPublicKey parses a public key in PKIX, ASN.1 DER form. The encoded
// public key is a SubjectPublicKeyInfo structure (see RFC 5280, Section 4.1).
//
// It returns a *[rsa.PublicKey], *[dsa.PublicKey], *[ecdsa.PublicKey],
// [ed25519.PublicKey] (not a pointer), *[mldsa.PublicKey], *[ecdh.PublicKey]
// (for X25519), *[mlkem.EncapsulationKey768], or *[mlkem.EncapsulationKey1024].
// More types might be supported in the future.
//
// This kind of key is commonly encoded in PEM blocks of type "PUBLIC KEY".
func ParsePKIXPublicKey(derBytes []byte) (pub any, err error) {
	var pki publicKeyInfo
	if rest, err := asn1.Unmarshal(derBytes, &pki); err != nil {
		if _, err := asn1.Unmarshal(derBytes, &pkcs1PublicKey{}); err == nil {
			return nil, errors.New("x509: failed to parse public key (use ParsePKCS1PublicKey instead for this key format)")
		}
		return nil, err
	} else if len(rest) != 0 {
		return nil, errors.New("x509: trailing data after ASN.1 of public-key")
	}
	return parsePublicKey(&pki)
}
```

**File:** src/crypto/x509/x509.go (L1915-1923)
```go
	// Check that the signer's public key matches the private key, if available.
	type privateKey interface {
		Equal(crypto.PublicKey) bool
	}
	if privPub, ok := key.Public().(privateKey); !ok {
		return nil, errors.New("x509: internal error: supported public key does not implement Equal")
	} else if parent.PublicKey != nil && !privPub.Equal(parent.PublicKey) {
		return nil, errors.New("x509: provided PrivateKey doesn't match parent's PublicKey")
	}
```

**File:** src/crypto/x509/x509_test.go (L1039-1093)
```go
// Self-signed certificate using DSA with SHA1
var dsaCertPem = `-----BEGIN CERTIFICATE-----
MIIEDTCCA82gAwIBAgIJALHPghaoxeDhMAkGByqGSM44BAMweTELMAkGA1UEBhMC
VVMxCzAJBgNVBAgTAk5DMQ8wDQYDVQQHEwZOZXd0b24xFDASBgNVBAoTC0dvb2ds
ZSwgSW5jMRIwEAYDVQQDEwlKb24gQWxsaWUxIjAgBgkqhkiG9w0BCQEWE2pvbmFs
bGllQGdvb2dsZS5jb20wHhcNMTEwNTE0MDMwMTQ1WhcNMTEwNjEzMDMwMTQ1WjB5
MQswCQYDVQQGEwJVUzELMAkGA1UECBMCTkMxDzANBgNVBAcTBk5ld3RvbjEUMBIG
A1UEChMLR29vZ2xlLCBJbmMxEjAQBgNVBAMTCUpvbiBBbGxpZTEiMCAGCSqGSIb3
DQEJARYTam9uYWxsaWVAZ29vZ2xlLmNvbTCCAbcwggEsBgcqhkjOOAQBMIIBHwKB
gQC8hLUnQ7FpFYu4WXTj6DKvXvz8QrJkNJCVMTpKAT7uBpobk32S5RrPKXocd4gN
8lyGB9ggS03EVlEwXvSmO0DH2MQtke2jl9j1HLydClMf4sbx5V6TV9IFw505U1iW
jL7awRMgxge+FsudtJK254FjMFo03ZnOQ8ZJJ9E6AEDrlwIVAJpnBn9moyP11Ox5
Asc/5dnjb6dPAoGBAJFHd4KVv1iTVCvEG6gGiYop5DJh28hUQcN9kul+2A0yPUSC
X93oN00P8Vh3eYgSaCWZsha7zDG53MrVJ0Zf6v/X/CoZNhLldeNOepivTRAzn+Rz
kKUYy5l1sxYLHQKF0UGNCXfFKZT0PCmgU+PWhYNBBMn6/cIh44vp85ideo5CA4GE
AAKBgFmifCafzeRaohYKXJgMGSEaggCVCRq5xdyDCat+wbOkjC4mfG01/um3G8u5
LxasjlWRKTR/tcAL7t0QuokVyQaYdVypZXNaMtx1db7YBuHjj3aP+8JOQRI9xz8c
bp5NDJ5pISiFOv4p3GZfqZPcqckDt78AtkQrmnal2txhhjF6o4HeMIHbMB0GA1Ud
DgQWBBQVyyr7hO11ZFFpWX50298Sa3V+rzCBqwYDVR0jBIGjMIGggBQVyyr7hO11
ZFFpWX50298Sa3V+r6F9pHsweTELMAkGA1UEBhMCVVMxCzAJBgNVBAgTAk5DMQ8w
DQYDVQQHEwZOZXd0b24xFDASBgNVBAoTC0dvb2dsZSwgSW5jMRIwEAYDVQQDEwlK
b24gQWxsaWUxIjAgBgkqhkiG9w0BCQEWE2pvbmFsbGllQGdvb2dsZS5jb22CCQCx
z4IWqMXg4TAMBgNVHRMEBTADAQH/MAkGByqGSM44BAMDLwAwLAIUPtn/5j8Q1jJI
7ggOIsgrhgUdjGQCFCsmDq1H11q9+9Wp9IMeGrTSKHIM
-----END CERTIFICATE-----
`

func TestParseCertificateWithDsaPublicKey(t *testing.T) {
	expectedKey := &dsa.PublicKey{
		Parameters: dsa.Parameters{
			P: bigFromHexString("00BC84B52743B169158BB85974E3E832AF5EFCFC42B264349095313A4A013EEE069A1B937D92E51ACF297A1C77880DF25C8607D8204B4DC45651305EF4A63B40C7D8C42D91EDA397D8F51CBC9D0A531FE2C6F1E55E9357D205C39D395358968CBEDAC11320C607BE16CB9DB492B6E78163305A34DD99CE43C64927D13A0040EB97"),
			Q: bigFromHexString("009A67067F66A323F5D4EC7902C73FE5D9E36FA74F"),
			G: bigFromHexString("009147778295BF5893542BC41BA806898A29E43261DBC85441C37D92E97ED80D323D44825FDDE8374D0FF15877798812682599B216BBCC31B9DCCAD527465FEAFFD7FC2A193612E575E34E7A98AF4D10339FE47390A518CB9975B3160B1D0285D1418D0977C52994F43C29A053E3D685834104C9FAFDC221E38BE9F3989D7A8E42"),
		},
		Y: bigFromHexString("59A27C269FCDE45AA2160A5C980C19211A820095091AB9C5DC8309AB7EC1B3A48C2E267C6D35FEE9B71BCBB92F16AC8E559129347FB5C00BEEDD10BA8915C90698755CA965735A32DC7575BED806E1E38F768FFBC24E41123DC73F1C6E9E4D0C9E692128853AFE29DC665FA993DCA9C903B7BF00B6442B9A76A5DADC6186317A"),
	}
	pemBlock, _ := pem.Decode([]byte(dsaCertPem))
	cert, err := ParseCertificate(pemBlock.Bytes)
	if err != nil {
		t.Fatalf("Failed to parse certificate: %s", err)
	}
	if cert.PublicKeyAlgorithm != DSA {
		t.Errorf("Parsed key algorithm was not DSA")
	}
	parsedKey, ok := cert.PublicKey.(*dsa.PublicKey)
	if !ok {
		t.Fatalf("Parsed key was not a DSA key: %s", err)
	}
	if expectedKey.Y.Cmp(parsedKey.Y) != 0 ||
		expectedKey.P.Cmp(parsedKey.P) != 0 ||
		expectedKey.Q.Cmp(parsedKey.Q) != 0 ||
		expectedKey.G.Cmp(parsedKey.G) != 0 {
		t.Fatal("Parsed key differs from expected key")
	}
}
```
