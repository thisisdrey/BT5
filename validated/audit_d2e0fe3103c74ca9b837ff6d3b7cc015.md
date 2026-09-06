### No vulnerability found for this question.

The comparison `auth_header != password` at [1](#0-0)  is a standard Rust `String`/`&str` `PartialEq`, which is a byte-exact comparison — no case-folding, trimming, or normalization is applied to either side.

Tracing the header value's origin, `preamble.headers.get("authorization")` returns the value exactly as parsed from the wire by `HttpRequestPreamble::consensus_deserialize`, which only lower-cases the header *name* (`req_header.name.to_lowercase()`) and validates the *value* is UTF-8/ASCII and under `HTTP_PREAMBLE_MAX_ENCODED_SIZE`, but never trims or otherwise mutates the value string itself before storing it in the `headers` map: [2](#0-1) . There is no code path between header parsing and the `auth_header != password` check that trims whitespace, so `" 12345"` will never be normalized to equal a configured secret of `"12345"` — the "auth bypass via whitespace normalization" half of the question does not exist in this codebase.

Regarding the other half (a byte-exact mismatch like `"12345 "` vs `"12345"` causing a legitimate request to be rejected with 401): this is indeed the current, intended behavior of strict equality, but it is not a security vulnerability under the given rules — it requires the operator/client to misconfigure or mistype the secret with trailing whitespace, is not attacker-triggerable against a correctly-configured node, and "valid-message-drop" is explicitly not one of the accepted Critical/High impact categories (no forged/relayed state, no unauthenticated write, no crash, no DoS from a few messages). Since neither the bypass condition nor an in-scope high/critical impact exists, this does not qualify as a valid finding under the audit criteria.

### Citations

**File:** stackslib/src/net/api/postblock_v3.rs (L104-110)
```rust
        if let Some(password) = &self.auth {
            if let Some(auth_header) = preamble.headers.get("authorization") {
                if auth_header != password {
                    return Err(Error::Http(401, "Unauthorized".into()));
                }
                authenticated = true;
            }
```

**File:** stackslib/src/net/http/request.rs (L355-421)
```rust
                for req_header in req.headers.iter() {
                    let value = String::from_utf8(req_header.value.to_vec()).map_err(|_e| {
                        CodecError::DeserializeError(
                            "Invalid HTTP header value: not utf-8".to_string(),
                        )
                    })?;
                    if !value.is_ascii() {
                        return Err(CodecError::DeserializeError(
                            "Invalid HTTP request: header value is not ASCII-US".to_string(),
                        ));
                    }
                    if value.len() > HTTP_PREAMBLE_MAX_ENCODED_SIZE as usize {
                        return Err(CodecError::DeserializeError(
                            "Invalid HTTP request: header value is too big".to_string(),
                        ));
                    }

                    let key = req_header.name.to_lowercase();

                    if seen_headers.contains(&key) {
                        return Err(CodecError::DeserializeError(format!(
                            "Invalid HTTP request: duplicate header \"{}\"",
                            key
                        )));
                    }

                    if key == "host" {
                        peerhost = match value.parse::<PeerHost>() {
                            Ok(ph) => Some(ph),
                            Err(_) => None,
                        };
                        seen_headers.insert(key);
                    } else if key == "content-type" {
                        // parse
                        let ctype = value.to_lowercase().parse::<HttpContentType>()?;
                        content_type = Some(ctype);
                        seen_headers.insert(key);
                    } else if key == "content-length" {
                        // parse
                        content_length = match value.parse::<u32>() {
                            Ok(len) => Some(len),
                            Err(_) => None,
                        };
                        seen_headers.insert(key);
                    } else if key == "connection" {
                        // parse
                        if value.to_lowercase() == "close" {
                            keep_alive = false;
                        } else if value.to_lowercase() == "keep-alive" {
                            keep_alive = true;
                        } else {
                            return Err(CodecError::DeserializeError(
                                "Inavlid HTTP request: invalid Connection: header".to_string(),
                            ));
                        }
                        seen_headers.insert(key);
                    } else if key == "set-cookie" {
                        set_cookie.push(value);
                    } else {
                        headers
                            .entry(key)
                            .and_modify(|entry| {
                                entry.push_str(", ");
                                entry.push_str(&value);
                            })
                            .or_insert(value);
                    }
```
