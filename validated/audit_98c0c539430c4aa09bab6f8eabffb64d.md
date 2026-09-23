### Title
ALPN negotiation error contains unescaped attacker-controlled protocol strings - (File: src/crypto/tls/handshake_server.go)

### Summary
`negotiateALPN` in `crypto/tls` builds its error message by directly interpolating the client-supplied ALPN protocol list into `fmt.Errorf` with `%q`. This client-hello `alpnProtocols` field is fully attacker-controlled and can contain non-printable/control characters or escape-like sequences, and the resulting error string propagates unescaped from `Conn.Handshake()` up to logging call sites such as `net/http`'s `"http: TLS handshake error from %s: %v"` log line, matching the same primitive as CVE-2025-58189/GO-2025-4008.

### Finding Description
An unauthenticated TLS client sends a ClientHello with an `application_layer_protocol_negotiation` extension containing attacker-chosen protocol name strings. The server parses these into `hs.clientHello.alpnProtocols` (`src/crypto/tls/handshake_messages.go`), and `serverHandshakeState.processClientHello` calls `negotiateALPN(c.config.NextProtos, hs.clientHello.alpnProtocols, false)` [1](#0-0)  and TLS 1.3's `processClientHello` calls the same function [2](#0-1) . When no protocol overlaps, `negotiateALPN` returns `fmt.Errorf("tls: client requested unsupported application protocols (%q)", clientProtos)` [3](#0-2) , embedding the raw, attacker-supplied strings (only `%q`-quoted, not otherwise sanitized for downstream consumers) into the error that is returned all the way out of `Conn.Handshake()`. Server code that logs handshake errors verbatim — e.g. `net/http`'s TLS accept-loop handshake error logging (`"http: TLS handshake error from %s: %v"`) — will therefore write attacker-controlled content into server logs without escaping, matching the reported bug class (an ALPN handshake error carrying unescaped attacker-controlled information) rather than merely a keyword match.

### Impact Explanation
This is a log/error-message information-integrity issue: an unauthenticated remote client can inject arbitrary (though length-bounded) byte content into server-side error logs via the ALPN extension, which can enable log forging/injection (e.g., fake log lines, terminal escape sequences, log parser confusion) if the log consumer doesn't sanitize. It does not by itself yield code execution or bypass authentication, so this aligns with a Go **PUBLIC** track (low/medium-severity information handling issue), consistent with the CVE's "Medium" severity rating.

### Likelihood Explanation
Any Go TLS server (e.g., an `http.Server` with `TLSConfig.NextProtos` set) processing ordinary internet traffic is reachable by any unauthenticated client that opens a TCP connection and sends a crafted ClientHello with a non-overlapping, specially-crafted ALPN protocol list — no privileges, valid certificate, or victim interaction beyond normal request handling are required.

### Recommendation
Sanitize/escape the client-supplied ALPN protocol strings before embedding them in the error returned by `negotiateALPN` (e.g., strip or quote non-printable bytes, or omit the raw protocol list entirely and rely on structured/attribute-based logging), matching the upstream fix in https://go.dev/cl/707776.

### Proof of Concept
```go
package tls

import (
	"strings"
	"testing"
)

func TestNegotiateALPNEscapesClientInput(t *testing.T) {
	malicious := []string{"proto\n[FAKE LOG LINE] admin login succeeded"}
	_, err := negotiateALPN([]string{"h2"}, malicious, false)
	if err == nil {
		t.Fatal("expected error")
	}
	// Attacker-controlled newline/content flows unescaped into the error string.
	if strings.Contains(err.Error(), "\n") {
		t.Fatalf("error message contains unescaped attacker-controlled newline: %q", err.Error())
	}
}
```
Expected (pre-fix) result: the test fails because `err.Error()` contains the raw attacker-supplied newline and injected text, demonstrating that `negotiateALPN`'s error carries unescaped attacker-controlled data that downstream log statements (e.g. `net/http`'s TLS handshake error logging) would write verbatim.

### Citations

**File:** src/crypto/tls/handshake_server.go (L265-269)
```go
	selectedProto, err := negotiateALPN(c.config.NextProtos, hs.clientHello.alpnProtocols, false)
	if err != nil {
		c.sendAlert(alertNoApplicationProtocol)
		return err
	}
```

**File:** src/crypto/tls/handshake_server.go (L333-363)
```go
// negotiateALPN picks a shared ALPN protocol that both sides support in server
// preference order. If ALPN is not configured or the peer doesn't support it,
// it returns "" and no error.
func negotiateALPN(serverProtos, clientProtos []string, quic bool) (string, error) {
	if len(serverProtos) == 0 || len(clientProtos) == 0 {
		if quic && len(serverProtos) != 0 {
			// RFC 9001, Section 8.1
			return "", fmt.Errorf("tls: client did not request an application protocol")
		}
		return "", nil
	}
	var http11fallback bool
	for _, s := range serverProtos {
		for _, c := range clientProtos {
			if s == c {
				return s, nil
			}
			if s == "h2" && c == "http/1.1" {
				http11fallback = true
			}
		}
	}
	// As a special case, let http/1.1 clients connect to h2 servers as if they
	// didn't support ALPN. We used not to enforce protocol overlap, so over
	// time a number of HTTP servers were configured with only "h2", but
	// expected to accept connections from "http/1.1" clients. See Issue 46310.
	if http11fallback {
		return "", nil
	}
	return "", fmt.Errorf("tls: client requested unsupported application protocols (%q)", clientProtos)
}
```

**File:** src/crypto/tls/handshake_server_tls13.go (L260-264)
```go
	selectedProto, err := negotiateALPN(c.config.NextProtos, hs.clientHello.alpnProtocols, c.quic != nil)
	if err != nil {
		c.sendAlert(alertNoApplicationProtocol)
		return err
	}
```
