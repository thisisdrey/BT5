`vcweb` is confirmed to be a test-only mock VCS server package used exclusively by `cmd/go`'s own test infrastructure (`vcweb/vcstest`), not a component of the shipped `go` toolchain that end users or unauthenticated attackers on the internet interact with. It's used by `go_test.go`, `coderepo_test.go`, `query_test.go`, and script tests as a local `httptest`-style fixture. Per the scan rules, test/mock-only paths are out of scope for this analysis.

## Findings

I searched the codebase for secret/password/token comparisons that could exhibit CVE-2017-5361's core defect — non-constant-time string comparison of authentication secrets reachable by an unauthenticated remote attacker:

1. **`crypto/subtle`, `crypto/tls`, `crypto/rsa`, `crypto/ecdh`, `crypto/internal/fips140/mlkem`** — All secret/MAC/Finished-message/ciphertext comparisons in production crypto and TLS code correctly use `subtle.ConstantTimeCompare` or the FIPS `constanttime` intrinsics package, e.g. the TLS record MAC check [1](#0-0) , the TLS 1.2 client Finished-message verification [2](#0-1) , RSA-OAEP padding validation [3](#0-2) , and ML-KEM re-encryption checks [4](#0-3) .

2. **`net/http`'s `Request.BasicAuth`/`parseBasicAuth`** just decodes and splits the header; it performs no comparison itself, leaving that responsibility to the caller [5](#0-4) . This is by design — the standard library doesn't ship a production HTTP Basic Auth *verifier*, so there's no internal net/http codepath doing an insecure `==` comparison of a real secret against attacker input.

3. The one place I found a plain `!=` string comparison of a password against a `BasicAuth`-derived value is `cmd/go/internal/vcweb/auth.go`'s `authHandler.Handler` [6](#0-5) , but `vcweb` is explicitly a mock VCS HTTP server built only for `cmd/go`'s own test suite (`vcweb/vcstest`), not code shipped to or run by end users [7](#0-6) . This falls under the "test/mock ... paths" exclusion in the scan rules.

I found no production, unauthenticated-remote-attacker-reachable Go stdlib/toolchain code path that compares secrets, passwords, or tokens using a non-constant-time algorithm. The standard library's actual security-sensitive comparisons (TLS handshake, MAC verification, padding oracle-prone RSA decryption, KEM re-encryption checks) consistently and correctly route through `crypto/subtle` or the FIPS constant-time primitives.

### No vulnerability found for this question.

### Citations

**File:** src/crypto/tls/conn.go (L459-467)
```go
		// This is equivalent to checking the MACs and paddingGood
		// separately, but in constant-time to prevent distinguishing
		// padding failures from MAC failures. Depending on what value
		// of paddingLen was returned on bad padding, distinguishing
		// bad MAC from bad padding can lead to an attack.
		//
		// See also the logic at the end of extractPadding.
		macAndPaddingGood := subtle.ConstantTimeCompare(localMAC, remoteMAC) & int(paddingGood)
		if macAndPaddingGood != 1 {
```

**File:** src/crypto/tls/handshake_client.go (L1009-1014)
```go
	verify := hs.finishedHash.serverSum(hs.masterSecret)
	if len(verify) != len(serverFinished.verifyData) ||
		subtle.ConstantTimeCompare(verify, serverFinished.verifyData) != 1 {
		c.sendAlert(alertHandshakeFailure)
		return errors.New("tls: server's Finished message was incorrect")
	}
```

**File:** src/crypto/internal/fips140/rsa/pkcs1v22.go (L446-469)
```go
	// We have to validate the plaintext in constant time in order to avoid
	// attacks like: J. Manger. A Chosen Ciphertext Attack on RSA Optimal
	// Asymmetric Encryption Padding (OAEP) as Standardized in PKCS #1
	// v2.0. In J. Kilian, editor, Advances in Cryptology.
	lHash2Good := subtle.ConstantTimeCompare(lHash, lHash2)

	// The remainder of the plaintext must be zero or more 0x00, followed
	// by 0x01, followed by the message.
	//   lookingForIndex: 1 iff we are still looking for the 0x01
	//   index: the offset of the first 0x01 byte
	//   invalid: 1 iff we saw a non-zero byte before the 0x01.
	var lookingForIndex, index, invalid int
	lookingForIndex = 1
	rest := db[hash.Size():]

	for i := 0; i < len(rest); i++ {
		equals0 := constanttime.ByteEq(rest[i], 0)
		equals1 := constanttime.ByteEq(rest[i], 1)
		index = constanttime.Select(lookingForIndex&equals1, i, index)
		lookingForIndex = constanttime.Select(equals1, 0, lookingForIndex)
		invalid = constanttime.Select(lookingForIndex&^equals0, 1, invalid)
	}

	if firstByteIsZero&lHash2Good&^invalid&^lookingForIndex != 1 {
```

**File:** src/crypto/internal/fips140/mlkem/mlkem768.go (L493-497)
```go
	if testingOnlyRejectionOutcome != nil {
		testingOnlyRejectionOutcome(subtle.ConstantTimeCompare(c[:], c1))
	}

	subtle.ConstantTimeCopy(subtle.ConstantTimeCompare(c[:], c1), Kout, Kprime)
```

**File:** src/net/http/request.go (L976-1015)
```go
// BasicAuth returns the username and password provided in the request's
// Authorization header, if the request uses HTTP Basic Authentication.
// See RFC 2617, Section 2.
func (r *Request) BasicAuth() (username, password string, ok bool) {
	auth := r.Header.Get("Authorization")
	if auth == "" {
		return "", "", false
	}
	return parseBasicAuth(auth)
}

// parseBasicAuth parses an HTTP Basic Authentication string.
// "Basic QWxhZGRpbjpvcGVuIHNlc2FtZQ==" returns ("Aladdin", "open sesame", true).
//
// parseBasicAuth should be an internal detail,
// but widely used packages access it using linkname.
// Notable members of the hall of shame include:
//   - github.com/sagernet/sing
//
// Do not remove or change the type signature.
// See go.dev/issue/67401.
//
//go:linkname parseBasicAuth
func parseBasicAuth(auth string) (username, password string, ok bool) {
	const prefix = "Basic "
	// Case insensitive prefix match. See Issue 22736.
	if len(auth) < len(prefix) || !ascii.EqualFold(auth[:len(prefix)], prefix) {
		return "", "", false
	}
	c, err := base64.StdEncoding.DecodeString(auth[len(prefix):])
	if err != nil {
		return "", "", false
	}
	cs := string(c)
	username, password, ok = strings.Cut(cs, ":")
	if !ok {
		return "", "", false
	}
	return username, password, true
}
```

**File:** src/cmd/go/internal/vcweb/auth.go (L93-93)
```go
		if username, password, ok := req.BasicAuth(); !ok || username != token.Username || password != token.Password {
```

**File:** src/cmd/go/internal/vcweb/vcweb.go (L1-13)
```go
// Copyright 2022 The Go Authors. All rights reserved.
// Use of this source code is governed by a BSD-style
// license that can be found in the LICENSE file.

// Package vcweb serves version control repos for testing the go command.
//
// It is loosely derived from golang.org/x/build/vcs-test/vcweb,
// which ran as a service hosted at vcs-test.golang.org.
//
// When a repository URL is first requested, the vcweb [Server] dynamically
// regenerates the repository using a script interpreted by a [script.Engine].
// The script produces the server's contents for a corresponding root URL and
// all subdirectories of that URL, which are then cached: subsequent requests
```
