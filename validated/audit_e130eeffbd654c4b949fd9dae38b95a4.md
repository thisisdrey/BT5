### Title
HTTP Request Smuggling via Quoted-String Chunk Extension Mis-parsing in `net/http/internal.chunkedReader` - (File: `src/net/http/internal/chunked.go`)

### Summary
Go's chunked-encoding parser terminates a chunk header line at the first raw `\r\n` byte pair it encounters, without regard to whether that `\r\n` occurs inside a quoted-string chunk-extension value. This is the exact bug class described in the Jetty advisory (GHSA-355h-qmc2-wpwf): per RFC 9112 §7.1.1 / RFC 9110 §5.6.4, a `chunk-ext-val` may be a `quoted-string`, and a `\r\n` inside the quotes must not terminate the chunk line. Because Go's implementation and any strictly RFC-compliant front-end/back-end peer will disagree on where the chunk header (and therefore the request) ends, request-smuggling / desync conditions can arise when Go's `net/http` server or client chunked codec is paired with another RFC-compliant HTTP implementation.

### Finding Description
Attacker-controlled bytes: a `Transfer-Encoding: chunked` HTTP request body containing a chunk-extension quoted-string value such as `1;a="` followed by a raw `\r\n`.

- Entry point: `internal.NewChunkedReader` is used by `net/http.readTransfer` to build the request/response body reader: `t.Body = &body{src: internal.NewChunkedReader(r), ...}` [1](#0-0) .
- `chunkedReader.beginChunk` calls `readChunkLine(cr.r)` to obtain the raw `chunk-size [chunk-ext] CRLF` line [2](#0-1) .
- `readChunkLine` reads up to the very first `\n` byte via `b.ReadSlice('\n')` and only validates that the sole `\r` in the slice sits immediately before that `\n` [3](#0-2) . This check enforces "one clean CRLF at the end of whatever ReadSlice returned," but it never distinguishes bytes inside a `"..."` quoted extension value from bytes that are structurally the end of the chunk header — so a `\r\n` embedded inside an open quoted-string is accepted as the legitimate line terminator.
- `removeChunkExtension` then simply does `bytes.Cut(p, semi)` and discards everything after the first `;`, again with no awareness of quoting [4](#0-3) .

The net effect: for input like `1;a="` + `\r\n` + `X` + `\r\n` + `0\r\n\r\n` + `GET /smuggled HTTP/1.1\r\n...`, Go's chunk decoder treats `1;a="` as a complete (if malformed-looking) chunk header, reads 1 byte of body (`X`), sees the terminating `0` chunk, and finishes decoding — leaving the trailing bytes (`GET /smuggled...`) untouched in the connection's shared `bufio.Reader`. Because `net/http.Server` reuses that same buffered reader for the next pipelined request on a keep-alive connection, those leftover bytes are parsed as an entirely new, attacker-smuggled HTTP request. An RFC-compliant peer in front of (or behind) a Go service — which would instead continue consuming the quoted-string across the `\r\n` and interpret the framing differently — will disagree with Go about where request N ends and request N+1 begins, which is the textbook HTTP request smuggling / desync primitive (CWE-444).

### Impact Explanation
This enables classic front-end/back-end request desynchronization: cache poisoning, security-control bypass, and cross-user response/session hijacking, wherever a Go `net/http` server or reverse-proxy component (`net/http/httputil`) is paired with any other HTTP implementation that correctly treats `\r\n` inside a chunk-extension quoted-string as non-terminating. This would fall under Go's PUBLIC track as a parsing-differential leading to smuggling, matching the severity class of the referenced Jetty CVE (CWE-444, High).

### Likelihood Explanation
The victim workflow is completely ordinary: any unauthenticated client can send a crafted `Transfer-Encoding: chunked` body to a Go HTTP server (directly, or through any load balancer/reverse proxy/CDN sitting in front of it) on a reused/pipelined connection. No credentials, TLS trust, or privileged access are required — only the ability to send one HTTP request.

### Recommendation
Rewrite `readChunkLine`/`removeChunkExtension` in `src/net/http/internal/chunked.go` to parse chunk extensions per RFC 9112 grammar: track quoted-string state (`quoted-pair`, escaped `\"`) while scanning, and only treat `\r\n` as the chunk-header terminator when it occurs outside of an open quoted-string; treat any bare/embedded `\r` or `\n` inside a quoted-string, or an unterminated quote, as a hard parse error instead of silently truncating the line.

### Proof of Concept
```go
package internal

import (
	"io"
	"strings"
	"testing"
)

// Demonstrates that a CRLF embedded in an *unterminated* chunk-extension
// quoted-string is accepted as the chunk-header terminator, instead of
// being rejected (or continuing to consume the quoted value across the
// CRLF), exactly the differential described in GHSA-355h-qmc2-wpwf.
func TestChunkExtensionQuotedStringCRLFDesync(t *testing.T) {
	// "1;a=\"" then a bare CRLF, then chunk data "X", then terminator.
	// RFC 9112 requires the quoted-string (and thus the chunk line) to
	// continue past this CRLF because the closing DQUOTE was never seen;
	// a spec-compliant peer would either keep consuming or error out.
	in := "1;a=\"\r\n" +
		"X\r\n" +
		"0\r\n\r\n" +
		"LEFTOVER-SMUGGLED-BYTES"

	r := strings.NewReader(in)
	cr := NewChunkedReader(r)

	data, err := io.ReadAll(cr)
	if err != nil {
		t.Fatalf("unexpected error decoding chunked body: %v", err)
	}
	if string(data) != "X" {
		t.Fatalf("decoded body = %q, want %q", data, "X")
	}

	// The vulnerability: despite the chunk-extension quote never being
	// closed, decoding succeeded and terminated cleanly, leaving the
	// attacker-controlled "LEFTOVER-SMUGGLED-BYTES" untouched in the
	// underlying reader. On a real net/http.Server connection, these
	// leftover bytes are exactly what gets parsed as the next pipelined
	// request -- the request-smuggling primitive.
	rest, _ := io.ReadAll(r)
	if string(rest) != "LEFTOVER-SMUGGLED-BYTES" {
		t.Fatalf("expected leftover bytes to remain unconsumed, got %q", rest)
	}
}
```
Expected assertion (current buggy behavior): decoding succeeds with body `"X"` and error `nil`, and `LEFTOVER-SMUGGLED-BYTES` remains in the stream unconsumed — proving the chunk header was terminated at the CRLF inside the open quoted-string rather than treated as a parse error or continuation of the extension value.

### Citations

**File:** src/net/http/transfer.go (L548-553)
```go
	case t.Chunked:
		if isResponse && (noResponseBodyExpected(t.RequestMethod) || !bodyAllowedForStatus(t.StatusCode)) {
			t.Body = NoBody
		} else {
			t.Body = &body{src: internal.NewChunkedReader(r), hdr: msg, r: r, closing: t.Close, maxTrailerHeaders: maxTrailerHeaders}
		}
```

**File:** src/net/http/internal/chunked.go (L46-58)
```go
func (cr *chunkedReader) beginChunk() {
	// chunk-size CRLF
	var line []byte
	line, cr.err = readChunkLine(cr.r)
	if cr.err != nil {
		return
	}
	cr.excess += int64(len(line)) + 2 // header, plus \r\n after the chunk data
	line = trimTrailingWhitespace(line)
	line, cr.err = removeChunkExtension(line)
	if cr.err != nil {
		return
	}
```

**File:** src/net/http/internal/chunked.go (L155-184)
```go
func readChunkLine(b *bufio.Reader) ([]byte, error) {
	p, err := b.ReadSlice('\n')
	if err != nil {
		// We always know when EOF is coming.
		// If the caller asked for a line, there should be a line.
		if err == io.EOF {
			err = io.ErrUnexpectedEOF
		} else if err == bufio.ErrBufferFull {
			err = ErrLineTooLong
		}
		return nil, err
	}

	// RFC 9112 permits parsers to accept a bare \n as a line ending in headers,
	// but not in chunked encoding lines. See https://www.rfc-editor.org/errata/eid7633,
	// which explicitly rejects a clarification permitting \n as a chunk terminator.
	//
	// Verify that the line ends in a CRLF, and that no CRs appear before the end.
	if idx := bytes.IndexByte(p, '\r'); idx == -1 {
		return nil, errors.New("chunked line ends with bare LF")
	} else if idx != len(p)-2 {
		return nil, errors.New("invalid CR in chunked line")
	}
	p = p[:len(p)-2] // trim CRLF

	if len(p) >= maxLineLength {
		return nil, ErrLineTooLong
	}
	return p, nil
}
```

**File:** src/net/http/internal/chunked.go (L199-212)
```go
// removeChunkExtension removes any chunk-extension from p.
// For example,
//
//	"0" => "0"
//	"0;token" => "0"
//	"0;token=val" => "0"
//	`0;token="quoted string"` => "0"
func removeChunkExtension(p []byte) ([]byte, error) {
	p, _, _ = bytes.Cut(p, semi)
	// TODO: care about exact syntax of chunk extensions? We're
	// ignoring and stripping them anyway. For now just never
	// return an error.
	return p, nil
}
```
