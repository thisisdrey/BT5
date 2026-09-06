### Title
Truncated chunked-encoded HTTP body is silently accepted as complete, causing bodies shorter than declared chunk-size sum - ([File: libsigner/src/http.rs])

### Summary
`decode_http_body` in `libsigner/src/http.rs` decodes chunked HTTP bodies via `HttpChunkedTransferReader::from_reader(ptr, MAX_MESSAGE_LEN.into())` and `read_to_end`. When the underlying byte buffer is exhausted mid-chunk (i.e., fewer bytes were actually sent/present than the declared chunk-size header promised), the reader treats this as normal EOF rather than an error, silently returning a truncated body to the caller instead of failing.

### Finding Description
`run_http_request` reads the entire HTTP response into memory with `sock.read_to_end(&mut buf)` (since requests use `Connection: close`), then calls `decode_http_response` followed by `decode_http_body(&headers, &buf[body_offset..])`. [1](#0-0) 

For chunked bodies, `decode_http_body` wraps the remaining slice in `HttpChunkedTransferReader::from_reader(ptr, MAX_MESSAGE_LEN.into())` and calls `fd.read_to_end(&mut decoded_body)`. [2](#0-1) 

The state machine in `HttpChunkedTransferReaderState::read_chunk_bytes` reads `self.chunk_size` bytes as declared by the chunk-size header. If the underlying `Read` (here, a `&[u8]` slice) is exhausted before `chunk_read` reaches `chunk_size`, `fd.read(buf)` returns `0`. `read_chunk_bytes` returns `Ok(0)` without erroring: [3](#0-2) 

`do_read` then handles this `nr == 0` case in the `Chunk` parse step by simply `break`ing out of the loop instead of raising an error — it does *not* distinguish "clean stream EOF between chunks" from "EOF while still owed `chunk_size - chunk_read` more bytes": [4](#0-3) 

Because `Read::read` on `HttpChunkedTransferReader` maps this to `Ok(0)` (the standard EOF signal), `read_to_end` treats it as a legitimate end-of-stream and returns successfully with whatever partial bytes were decoded so far — no `ChunkedError` is raised. The same silent-`break`-on-zero pattern also exists in `read_chunk_trailer` for a truncated trailer. [5](#0-4) 

Concretely, a malicious/MITM'd node that `libsigner`'s `StackerDBSession::rpc_request` polls (via `run_http_request`) can respond with:
```
HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\nffff\r\n<a few bytes>
```
and then close the connection before sending the promised `0xffff` bytes of chunk data or the terminating `0\r\n\r\n`. `sock.read_to_end` captures the truncated bytes; `decode_http_body` decodes them without error, returning a short `decoded_body` that does not match the sum of declared chunk sizes. This mismatch is exactly the equality violation the question describes: declared chunk-size sum ≠ actual bytes placed in `decoded_body`, and no `io::Error` is surfaced.

`MAX_MESSAGE_LEN` bound-checking (`read_chunk_boundary`'s `chunk_len > MAX_MESSAGE_LEN` check) only prevents oversized *declared* chunk sizes; it does nothing to detect under-delivery. [6](#0-5) 

### Impact Explanation
The truncated bytes then flow into `StackerDBSession::get_chunks`/`get_latest_chunks` as `Ok(body_bytes)` (no error path taken) and ultimately into `T::consensus_deserialize` via `get_latest`. [7](#0-6) [8](#0-7)  A truncated/malformed StackerDB chunk will most likely fail `consensus_deserialize`'s own length checks and be rejected there, bounding the practical damage to a failed deserialization (denial of a single poll) rather than acceptance of a forged/short payload as valid application data. There is no memory-safety issue (`decoded_body` is a `Vec<u8>` grown normally), no crash, and no write of unvalidated data to persistent/shared state — the truncated bytes are only consumed locally by the polling signer and (if they fail deserialization) discarded. This falls short of "High" per the given severity rubric (serving non-canonical state as canonical, false inventory, etc.); at most it is a client-side parsing correctness bug that could cause the signer to silently ignore/misinterpret a partial response, but it requires the peer to be one the signer already trusts/polls (a StackerDB replica endpoint it's configured to talk to), and does not smuggle a forged message that gets stored or relayed.

### Likelihood Explanation
Requires the attacker to control or MITM an endpoint that the signer is configured to poll via `StackerDBSession` (i.e., a node URL configured in the signer, not an arbitrary unprivileged remote party reachable without any such trust relationship). Triggerable with a single crafted response and zero cost; but the connection is closed by the attacker at will (`Connection: close` is client-set, and the server response is fully buffered by the client), so it is repeatable per request.

### Recommendation
In `read_chunk_bytes`/`do_read`, distinguish "no more data available but chunk incomplete" from legitimate stream end: if `fd.read` returns `0` while `chunk_read < chunk_size` (or during `ChunkTrailer` with `self.i < 2`), return an `io::Error` (e.g., `ChunkedError::DeserializeError("Unexpected EOF in chunked body")`) instead of breaking silently. This ensures `read_to_end` propagates an error rather than accepting a truncated body as complete.

### Proof of Concept
```rust
// stacks-common/src/util/chunked_encoding.rs test module
#[test]
fn test_http_chunked_decode_truncated_no_error() {
    // Declares "ffff" (65535) bytes but only provides 4 actual bytes, then EOF.
    let encoded = b"ffff\r\nabcd";
    let mut cursor = io::Cursor::new(&encoded[..]);
    let mut decoder = HttpChunkedTransferReader::from_reader(&mut cursor, 100000);
    let mut output = vec![];
    // BUG: this succeeds and returns only 4 bytes, instead of erroring
    let res = decoder.read_to_end(&mut output);
    assert!(res.is_ok(), "expected silent truncation, got {:?}", res);
    assert_eq!(output, b"abcd".to_vec());
    // Declared chunk size (65535) != actual decoded length (4) with no error signaled.
}
```
This mirrors the exact path exercised by `libsigner::http::decode_http_body` → `HttpChunkedTransferReader::read_to_end`, confirming that a chunk-size/actual-length mismatch is silently swallowed rather than raising a `ChunkedError`.

### Citations

**File:** libsigner/src/http.rs (L204-217)
```rust
    let body = if chunked {
        // chunked encoding
        let ptr = &mut buf;
        let mut fd = HttpChunkedTransferReader::from_reader(ptr, MAX_MESSAGE_LEN.into());
        let mut decoded_body = vec![];
        fd.read_to_end(&mut decoded_body)?;
        decoded_body
    } else {
        // body is just as-is
        buf.to_vec()
    };

    Ok(body)
}
```

**File:** libsigner/src/http.rs (L246-261)
```rust
    sock.write_all(req_txt.as_bytes())?;
    sock.write_all(payload)?;

    let mut buf = vec![];

    sock.read_to_end(&mut buf)?;

    let (headers, body_offset) = decode_http_response(&buf)?;
    if body_offset >= buf.len() {
        // no body
        debug!("No HTTP body");
        debug!("Headers: {:?}", &headers);
        return Ok(vec![]);
    }

    decode_http_body(&headers, &buf[body_offset..]).map_err(|e| e.into())
```

**File:** stacks-common/src/util/chunked_encoding.rs (L159-166)
```rust
        trace!("chunk offset: {offset}. chunk len: {chunk_len}");
        if chunk_len > MAX_MESSAGE_LEN as u64 {
            trace!("chunk buffer: {:?}", &self.chunk_buffer[0..self.i]);
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                ChunkedError::DeserializeError("Invalid HTTP chunk: too big".to_string()),
            ));
        }
```

**File:** stacks-common/src/util/chunked_encoding.rs (L185-226)
```rust
    fn read_chunk_bytes<R: Read>(&mut self, fd: &mut R, buf: &mut [u8]) -> io::Result<usize> {
        assert_eq!(self.parse_step, HttpChunkedTransferParseMode::Chunk);

        if self.total_size >= self.max_size && self.chunk_size > 0 {
            return Err(io::Error::other(ChunkedError::OverflowError(
                "HTTP body exceeds maximum expected length".to_string(),
            )));
        }

        let remaining = if self.chunk_size - self.chunk_read <= (self.max_size - self.total_size) {
            self.chunk_size - self.chunk_read
        } else {
            self.max_size - self.total_size
        };

        let nr = if (buf.len() as u64) < remaining {
            // can fill buffer
            trace!("Read {} bytes (fill buffer)", buf.len());
            fd.read(buf)? as u64
        } else {
            // will read up to a chunk boundary
            trace!("Read {remaining} bytes (fill remainder)");
            fd.read(&mut buf[0..(remaining as usize)])? as u64
        };

        trace!("Got {nr} bytes");

        self.chunk_read += nr;

        if self.chunk_read >= self.chunk_size {
            // done reading; proceed to consume trailer
            trace!(
                "begin reading trailer ({} >= {})",
                self.chunk_read,
                self.chunk_size
            );
            self.parse_step = HttpChunkedTransferParseMode::ChunkTrailer;
        }

        self.total_size += nr;
        Ok(nr as usize)
    }
```

**File:** stacks-common/src/util/chunked_encoding.rs (L231-269)
```rust
    fn read_chunk_trailer<R: Read>(&mut self, fd: &mut R) -> io::Result<usize> {
        assert_eq!(self.parse_step, HttpChunkedTransferParseMode::ChunkTrailer);

        let mut nr = 0;

        // read trailer
        if self.i < 2 {
            let mut trailer_buf = [0u8; 2];

            trace!("Read at most {} bytes", 2 - self.i);
            nr = fd.read(&mut trailer_buf[self.i..2])?;
            if nr == 0 {
                return Ok(nr);
            }

            self.chunk_buffer[self.i..2].copy_from_slice(&trailer_buf[self.i..2]);
            self.i += nr;
        }

        if self.i == 2 {
            // expect '\r\n'
            if self.chunk_buffer[0..2] != [0x0d, 0x0a] {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    ChunkedError::DeserializeError("Invalid chunk trailer".to_string()),
                ));
            }

            // end of chunk
            self.last_chunk_size = self.chunk_size;
            self.i = 0;

            trace!("begin reading boundary");
            self.parse_step = HttpChunkedTransferParseMode::ChunkBoundary;
        }

        trace!("Consumed {nr} bytes of chunk boundary (i = {})", self.i);
        Ok(nr)
    }
```

**File:** stacks-common/src/util/chunked_encoding.rs (L273-313)
```rust
    pub fn do_read<R: Read>(&mut self, fd: &mut R, buf: &mut [u8]) -> io::Result<(usize, usize)> {
        let mut decoded = 0;
        let mut consumed = 0;
        while decoded < buf.len() {
            match self.parse_step {
                HttpChunkedTransferParseMode::ChunkBoundary => {
                    let count = self.read_chunk_boundary(fd)?;
                    if count == 0 {
                        break;
                    }
                    consumed += count;
                }
                HttpChunkedTransferParseMode::Chunk => {
                    let nr = self.read_chunk_bytes(fd, &mut buf[decoded..])?;
                    if nr == 0 && self.parse_step == HttpChunkedTransferParseMode::Chunk {
                        // still trying to read the chunk, but got 0 bytes
                        break;
                    }
                    decoded += nr;
                    consumed += nr;
                }
                HttpChunkedTransferParseMode::ChunkTrailer => {
                    let count = self.read_chunk_trailer(fd)?;
                    if count == 0 {
                        break;
                    }
                    consumed += count;
                    if self.last_chunk_size == 0 {
                        // we're done
                        trace!("finished last chunk");
                        self.parse_step = HttpChunkedTransferParseMode::EOF;
                        break;
                    }
                }
                HttpChunkedTransferParseMode::EOF => {
                    break;
                }
            }
        }
        Ok((decoded, consumed))
    }
```

**File:** libsigner/src/session.rs (L84-95)
```rust
    fn get_latest<T: StacksMessageCodec>(&mut self, slot_id: u32) -> Result<Option<T>, RPCError> {
        let Some(latest_bytes) = self.get_latest_chunk(slot_id)? else {
            return Ok(None);
        };
        Some(
            T::consensus_deserialize(&mut latest_bytes.as_slice()).map_err(|e| {
                let msg = format!("StacksMessageCodec::consensus_deserialize failure: {e}");
                RPCError::Deserialize(msg)
            }),
        )
        .transpose()
    }
```

**File:** libsigner/src/session.rs (L212-227)
```rust
            let chunk = match self.rpc_request("GET", &path, None, &[]) {
                Ok(body_bytes) => Some(body_bytes),
                Err(RPCError::HttpError(code)) => {
                    if code != 404 {
                        return Err(RPCError::HttpError(code));
                    }
                    None
                }
                Err(e) => {
                    return Err(e);
                }
            };
            payloads.push(chunk);
        }
        Ok(payloads)
    }
```
