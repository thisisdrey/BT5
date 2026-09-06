[1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** libsigner/src/http.rs (L62-65)
```rust
pub fn decode_http_request(payload: &[u8]) -> Result<SignerHttpRequest, EventError> {
    // realistically, there won't be more than 32 headers
    let mut headers_buf = [httparse::EMPTY_HEADER; MAX_HTTP_HEADERS];
    let mut req = httparse::Request::new(&mut headers_buf);
```

**File:** libsigner/src/http.rs (L92-114)
```rust
            for i in 0..req.headers.len() {
                let value = String::from_utf8(req.headers[i].value.to_vec()).map_err(|_e| {
                    EventError::MalformedRequest("Invalid HTTP header value: not utf-8".to_string())
                })?;
                if !value.is_ascii() {
                    return Err(EventError::MalformedRequest(
                        "Invalid HTTP request: header value is not ASCII-US".to_string(),
                    ));
                }
                if value.len() > MAX_HTTP_HEADER_LEN {
                    return Err(EventError::MalformedRequest(
                        "Invalid HTTP request: header value is too big".to_string(),
                    ));
                }

                let key = req.headers[i].name.to_string().to_lowercase();
                if headers.get(&key).is_some() {
                    return Err(EventError::MalformedRequest(format!(
                        "Invalid HTTP request: duplicate header \"{key}\""
                    )));
                }
                headers.insert(key, value);
            }
```

**File:** libsigner/src/http.rs (L116-120)
```rust
        } else {
            return Err(EventError::Deserialize(
                "Failed to decode HTTP headers".to_string(),
            ));
        };
```
