Based on my investigation, Go's `net/http` package already has robust, longstanding protections against exactly this class of vulnerability (HTTP response splitting via CRLF injection in headers).

Key findings:
- `Header.writeSubset` in `src/net/http/header.go` applies `headerNewlineToSpace` (a `strings.Replacer` that converts `\n`/`\r` to spaces) to every header value before writing it to the wire, and validates header names with `httpguts.ValidHeaderFieldName`, dropping invalid ones entirely. [1](#0-0) [2](#0-1) 

- This exact defense was added specifically to close response-splitting/header-injection ("Header value with a newline character") issues, tracked as Issue 914, and is covered by `TestResponseWrite` and `TestHeaderWrite` regression tests. [3](#0-2) [4](#0-3) 

- The server-side request path additionally rejects control characters (including `\r`, `\n`, NUL, DEL) in header names/values at parse time, returning 400, per `TestServerValidatesHeaders`. [5](#0-4) 

- More recent hardening (Issue #78775) extended the same control-character rejection to Trailer field names/values in request writing. [6](#0-5) 

CVE-2017-7443 concerns apt-cacher(-ng)'s failure to block `%0[ad]`-encoded newlines in proxied HTTP responses — a distinct C/C++ codebase with no analogous unguarded sink in this Go standard library. The corresponding Go primitive (header value/name sanitization in `net/http`) already strips/rejects CR and LF at the exact write sink, so the bug class does not reproduce here.

### No Vulnerability found for this question.

### Citations

**File:** src/net/http/header.go (L139-139)
```go
var headerNewlineToSpace = strings.NewReplacer("\n", " ", "\r", " ")
```

**File:** src/net/http/header.go (L190-213)
```go
func (h Header) writeSubset(w io.Writer, exclude map[string]bool, trace *httptrace.ClientTrace) error {
	ws, ok := w.(io.StringWriter)
	if !ok {
		ws = stringWriter{w}
	}
	kvs, sorter := h.sortedKeyValues(exclude)
	var formattedVals []string
	for _, kv := range kvs {
		if !httpguts.ValidHeaderFieldName(kv.key) {
			// This could be an error. In the common case of
			// writing response headers, however, we have no good
			// way to provide the error back to the server
			// handler, so just drop invalid headers instead.
			continue
		}
		for _, v := range kv.values {
			v = headerNewlineToSpace.Replace(v)
			v = textproto.TrimString(v)
			for _, s := range []string{kv.key, ": ", v, "\r\n"} {
				if _, err := ws.WriteString(s); err != nil {
					headerSorterPool.Put(sorter)
					return err
				}
			}
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

**File:** src/net/http/header_test.go (L93-105)
```go
	// Tests invalid characters in headers.
	{
		Header{
			"Content-Type":             {"text/html; charset=UTF-8"},
			"NewlineInValue":           {"1\r\nBar: 2"},
			"NewlineInKey\r\n":         {"1"},
			"Colon:InKey":              {"1"},
			"Evil: 1\r\nSmuggledValue": {"1"},
		},
		nil,
		"Content-Type: text/html; charset=UTF-8\r\n" +
			"NewlineInValue: 1  Bar: 2\r\n",
	},
```

**File:** src/net/http/serve_test.go (L5412-5449)
```go
// Test that we validate the valid bytes in HTTP/1 headers.
// Issue 11207.
func TestServerValidatesHeaders(t *testing.T) {
	setParallel(t)
	tests := []struct {
		header string
		want   int
	}{
		{"", 200},
		{"Foo: bar\r\n", 200},
		{"X-Foo: bar\r\n", 200},
		{"Foo: a space\r\n", 200},

		{"A space: foo\r\n", 400},                            // space in header
		{"foo\xffbar: foo\r\n", 400},                         // binary in header
		{"foo\x00bar: foo\r\n", 400},                         // binary in header
		{"Foo: " + strings.Repeat("x", 1<<21) + "\r\n", 431}, // header too large
		// Spaces between the header key and colon are not allowed.
		// See RFC 7230, Section 3.2.4.
		{"Foo : bar\r\n", 400},
		{"Foo\t: bar\r\n", 400},

		// Empty header keys are invalid.
		// See RFC 7230, Section 3.2.
		{": empty key\r\n", 400},

		// Requests with invalid Content-Length headers should be rejected
		// regardless of the presence of a Transfer-Encoding header.
		// Check out RFC 9110, Section 8.6 and RFC 9112, Section 6.3.3.
		{"Content-Length: notdigits\r\n", 400},
		{"Content-Length: notdigits\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n", 400},

		{"foo: foo foo\r\n", 200},    // LWS space is okay
		{"foo: foo\tfoo\r\n", 200},   // LWS tab is okay
		{"foo: foo\x00foo\r\n", 400}, // CTL 0x00 in value is bad
		{"foo: foo\x7ffoo\r\n", 400}, // CTL 0x7f in value is bad
		{"foo: foo\xfffoo\r\n", 200}, // non-ASCII high octets in value are fine
	}
```

**File:** src/net/http/requestwrite_test.go (L641-683)
```go
	// Trailer names with control characters must not reach the wire,
	// where they would permit header injection on the "Trailer:" line.
	// Issue #78775
	28: {
		Req: Request{
			Method: "POST",
			URL: &url.URL{
				Scheme: "http",
				Host:   "example.com",
				Path:   "/",
			},
			ProtoMajor:       1,
			ProtoMinor:       1,
			Header:           Header{},
			TransferEncoding: []string{"chunked"},
			Trailer:          Header{"X-Trailer\r\nInjected: 1": {"ok"}},
		},

		Body: []byte("abcdef"),

		WantError: errors.New(`net/http: invalid trailer field name "X-Trailer\r\nInjected: 1"`),
	},

	// Trailer values with control characters are rejected as well. Issue #78775
	29: {
		Req: Request{
			Method: "POST",
			URL: &url.URL{
				Scheme: "http",
				Host:   "example.com",
				Path:   "/",
			},
			ProtoMajor:       1,
			ProtoMinor:       1,
			Header:           Header{},
			TransferEncoding: []string{"chunked"},
			Trailer:          Header{"X-Trailer": {"evil\r\nInjected: 1"}},
		},

		Body: []byte("abcdef"),

		WantError: errors.New(`net/http: invalid trailer field value for "X-Trailer"`),
	},
```
