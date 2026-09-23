No analogous vulnerability exists in this Go standard library mirror. Go's `net/http` package already defends against exactly this bug class.

The Keycloak issue stems from an admin being able to insert raw newlines into HTTP response headers, causing the server to write extra bytes to an already-terminated response, corrupting the stream and causing request failures. In Go's standard library, `net/http.Header.writeSubset` (the function that serializes headers onto the wire) explicitly guards against this: every header value is passed through `headerNewlineToSpace`, a `strings.Replacer` that converts embedded `\n`/`\r` bytes to spaces before the value is written, and the value is trimmed with `textproto.TrimString`. [1](#0-0) [2](#0-1) 

Header field *names* go through a separate check, `httpguts.ValidHeaderFieldName`, and are silently dropped if invalid rather than written malformed: [3](#0-2) 

This means it is structurally impossible for a caller (even one fully controlling arbitrary header values, analogous to the "admin changes realm/security headers" premise in the advisory) to inject a literal CRLF sequence that would prematurely terminate the header block or desynchronize the writer — the newline is neutralized before any I/O happens. This exact case is covered by a regression test (`Issue 914`) that confirms a `\n` in a header value is converted to a space rather than being written raw: [4](#0-3) 

Separately, for trailers (a different write path), Go rejects invalid names/values outright with an error instead of writing them, per Issue #78775: [5](#0-4) 

The HTTP/2 server path (`internal/http2`) similarly rejects newline/CR/NUL bytes in field values at the point of request parsing (`TestServer_Request_Reject_HeaderFieldValueNewline`, `...CR`) and drops invalid outgoing header names/values (`TestServerDoesntWriteInvalidHeaders`) rather than emitting a corrupted frame: [6](#0-5) [7](#0-6) 

Because there is no reachable code path where an untrusted/attacker-influenced header value with embedded CR/LF reaches the wire unsanitized, there is no analog to the Keycloak DoS primitive (writing to an already-terminated request due to injected newlines) in this codebase.

### No vulnerability found for this question.

### Citations

**File:** src/net/http/header.go (L139-139)
```go
var headerNewlineToSpace = strings.NewReplacer("\n", " ", "\r", " ")
```

**File:** src/net/http/header.go (L196-204)
```go
	var formattedVals []string
	for _, kv := range kvs {
		if !httpguts.ValidHeaderFieldName(kv.key) {
			// This could be an error. In the common case of
			// writing response headers, however, we have no good
			// way to provide the error back to the server
			// handler, so just drop invalid headers instead.
			continue
		}
```

**File:** src/net/http/header.go (L205-207)
```go
		for _, v := range kv.values {
			v = headerNewlineToSpace.Replace(v)
			v = textproto.TrimString(v)
```

**File:** src/net/http/responsewrite_test.go (L171-192)
```go
		// Header value with a newline character (Issue 914).
		// Also tests removal of leading and trailing whitespace.
		{
			Response{
				StatusCode: 204,
				ProtoMajor: 1,
				ProtoMinor: 1,
				Request:    dummyReq("GET"),
				Header: Header{
					"Foo": []string{" Bar\nBaz "},
				},
				Body:             nil,
				ContentLength:    0,
				TransferEncoding: []string{"chunked"},
				Close:            true,
			},

			"HTTP/1.1 204 No Content\r\n" +
				"Connection: close\r\n" +
				"Foo: Bar Baz\r\n" +
				"\r\n",
		},
```

**File:** src/net/http/responsewrite_test.go (L292-311)
```go
// Response.Write shares the trailer validation added for Issue #78775 with
// Request.Write, so an invalid trailer name or value must be rejected rather
// than written.
func TestResponseWriteInvalidTrailer(t *testing.T) {
	tests := []struct {
		name    string
		trailer Header
		wantErr string
	}{
		{
			name:    "key",
			trailer: Header{"X-Trailer\r\nInjected: 1": {"ok"}},
			wantErr: `net/http: invalid trailer field name "X-Trailer\r\nInjected: 1"`,
		},
		{
			name:    "value",
			trailer: Header{"X-Trailer": {"evil\r\nInjected: 1"}},
			wantErr: `net/http: invalid trailer field value for "X-Trailer"`,
		},
	}
```

**File:** src/net/http/internal/http2/server_test.go (L1071-1077)
```go
func TestServer_Request_Reject_HeaderFieldValueNewline(t *testing.T) {
	testRejectRequest(t, func(st *serverTester) { st.bodylessReq1("foo", "has\nnewline") })
}

func TestServer_Request_Reject_HeaderFieldValueCR(t *testing.T) {
	testRejectRequest(t, func(st *serverTester) { st.bodylessReq1("foo", "has\rcarriage") })
}
```

**File:** src/net/http/internal/http2/server_test.go (L3347-3371)
```go
// validate transmitted header field names & values
// golang.org/issue/14048
func TestServerDoesntWriteInvalidHeaders(t *testing.T) {
	synctest.Test(t, testServerDoesntWriteInvalidHeaders)
}
func testServerDoesntWriteInvalidHeaders(t *testing.T) {
	testServerResponse(t, func(w http.ResponseWriter, r *http.Request) error {
		w.Header().Add("OK1", "x")
		w.Header().Add("Bad:Colon", "x") // colon (non-token byte) in key
		w.Header().Add("Bad1\x00", "x")  // null in key
		w.Header().Add("Bad2", "x\x00y") // null in value
		return nil
	}, func(st *serverTester) {
		getSlash(st)
		st.wantHeaders(wantHeader{
			streamID:  1,
			endStream: true,
			header: http.Header{
				":status":        []string{"200"},
				"ok1":            []string{"x"},
				"content-length": []string{"0"},
			},
		})
	})
}
```
