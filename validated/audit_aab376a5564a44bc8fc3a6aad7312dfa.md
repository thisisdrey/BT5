### Title
Unbounded in-memory expansion during `Value::deserialize_read` bypasses the parse-time memory budget - ([File: stackslib/src/net/api/read_only/parse.rs])

### Summary
`parse_read_only_call_body` (used by both the unauthenticated `/v2/contracts/call-read` endpoint and the authenticated `/v3/contracts/fast-call-read` endpoint) preflights only the size of the raw hex-decoded byte buffer before calling `Value::deserialize_read`, then checks the resulting retained memory only after `deserialize_read` returns. Because the Clarity binary wire format is far more compact than the in-memory `Value`/`Vec<Value>` representation it produces (e.g. one wire byte per `Bool`/`OptionalNone` list element vs. a full `Value` enum entry per element), a single crafted argument can cause `deserialize_read` to allocate far beyond `read_only_call_max_mem_bytes` before the post-hoc checkpoint has a chance to reject it.

### Finding Description
The claimed equality/fault holds: bytes retained by parsing are supposed to be bounded *during* parsing, but the code only bounds the wire-side allocation and measures the resulting Value-tree allocation *after* it has already happened.

Code path:
1. `RPCFastCallReadOnlyRequestHandler::try_parse_request` (and identically `RPCCallReadOnlyRequestHandler::try_parse_request`) gate only on wire length: `content_len < maximum_call_argument_size` [1](#0-0) , then call `parse_read_only_call_body(body, read_only_call_max_mem_bytes)` [2](#0-1) .
2. Inside, `deserialize_arguments` → `deserialize_value` preflights only `(hex.len() as u64).div_ceil(2)` — the size of the *raw decoded byte buffer* — before calling `hex_bytes(hex)` and then `Value::deserialize_read`, and only calls `limiter.checkpoint()` *after* `deserialize_read` returns fully built: [3](#0-2) .
3. `Value::deserialize_read` builds an entire `Value` tree from that byte buffer with no intermediate budget checks: for a `List`, it reads a declared item count `len` bounded only by `len > MAX_VALUE_SIZE` (a byte-size constant, not an item-count constant) and then repeatedly `items.push(item)` for every element until the whole list is materialized [4](#0-3) [5](#0-4) . Each wire-cheap element (e.g. 1-byte `BoolTrue`/`OptionalNone` prefix) becomes a full `Value` enum entry in the growing `Vec<Value>`, which is many bytes larger than its wire encoding — no `check_can_allocate` call occurs anywhere inside this loop.
4. `ParseLimiter`/`ResourceLimiter` do rely on a real global-allocator hook (`TrackingAllocator`) that increments a thread-local counter on every `alloc`/`realloc` [6](#0-5) , but that counter is only *read and compared* at explicit `preflight`/`checkpoint` call sites [7](#0-6)  — there is no enforcement between those call sites. Since the entire `Value::deserialize_read` call happens between one preflight (on the small hex buffer) and the next checkpoint, the multiplication into a much larger `Vec<Value>` tree runs unchecked.

The `test_read_only_parse_records_retained_mem` test in `stacks-node/src/tests/mem_abort.rs` only asserts that `parse_retained_mem_bytes` is *recorded* after the fact [8](#0-7) ; it does not assert that the transient peak allocation during parsing stayed within the configured budget, which is exactly the property this finding shows is broken.

Critically, this same `parse_read_only_call_body` function is shared by the **unauthenticated** `/v2/contracts/call-read` endpoint (`RPCCallReadOnlyRequestHandler::try_parse_request`) [9](#0-8) , so no `authorization` header or secret is needed at all to trigger this — it is reachable by a fully unprivileged remote attacker, which is stronger than the fast-call-read premise in the question.

### Impact Explanation
A single crafted read-only call request causes the node's Clarity-arg-parsing code to transiently allocate memory far beyond the configured `read_only_call_max_mem_bytes` budget before the check that is supposed to bound it runs. Repeated concurrently from multiple unprivileged connections, this is a bounded-compute-DoS primitive against a remote-facing read endpoint (matches the "bounded compute DoS on a read endpoint" High-severity category); in the worst case it can drive real memory pressure/OOM on the node process, which is a heavier outcome than the budget was designed to prevent.

### Likelihood Explanation
No privileged role, secret, or peer/config state is required: the vulnerable code path is reachable through the completely unauthenticated `/v2/contracts/call-read/...` endpoint, and it is also reachable through `/v3/contracts/fast-call-read` once (as the question stipulates) the attacker knows the shared `authorization` secret. The attack requires only one crafted, well-formed HTTP POST body whose `content_len` fits under `maximum_call_argument_size`, and it is repeatable per request/connection at essentially zero attacker cost (the compact wire-encoding-to-heap amplification does the work).

### Recommendation
Enforce the memory budget *during* `Value::deserialize_read`, not only via preflight-on-raw-bytes plus a single post-hoc checkpoint:
- Pass the `ResourceLimiter`/budget into `Value::deserialize_read`'s stack-based loop and call `check_can_allocate` (or an equivalent periodic check) each time an item is pushed into a `List`/`Tuple` frame, so growth is bounded as it happens.
- Alternatively, bound `len` for `TypePrefix::List`/`TypePrefix::Tuple` by a worst-case in-memory size (`len * size_of::<Value>()`, plus recursive worst case for nested types) against the remaining parse budget before starting to push items, rather than only against the byte-size constant `MAX_VALUE_SIZE`.
- Ensure the fix applies to both `/v2/contracts/call-read` and `/v3/contracts/fast-call-read`, since both funnel through `parse_read_only_call_body`/`deserialize_value`.

### Proof of Concept
Extend the pattern used in `stacks-node/src/tests/mem_abort.rs::try_parse_call_read`:
1. Build a hex-encoded Clarity `List` argument whose wire encoding is small (e.g. a `List` prefix + 4-byte length declaring N elements, each encoded as the 1-byte `OptionalNone`/`BoolTrue` prefix), keeping total hex/body length well under both `maximum_call_argument_size` and a small configured `read_only_call_max_mem_bytes` (e.g. a few KB).
2. Choose N large enough that `N * size_of::<Value>()` (the resulting `Vec<Value>` heap size) exceeds `read_only_call_max_mem_bytes` by a large factor, while the wire body itself (`hex.len()/2`) stays under it.
3. Call `RPCCallReadOnlyRequestHandler::try_parse_request` (or `RPCFastCallReadOnlyRequestHandler::try_parse_request` with a valid `authorization` header) with this body and a small `read_only_call_max_mem_bytes`.
4. Assert that either (a) the call succeeds despite the true peak allocation during `Value::deserialize_read` exceeding the configured budget (observable via `thread_allocated()`/`AllocationCounter` peak sampling around the call), or (b) instrument `deserialize_value`/`Value::deserialize_read` to record peak thread-allocated bytes and assert `peak > read_only_call_max_mem_bytes` even though `try_parse_request` only errors (via `checkpoint()`) after that peak has already been reached — demonstrating the budget is enforced after the fact rather than during allocation as intended.

### Citations

**File:** stackslib/src/net/api/fastcallreadonly.rs (L112-120)
```rust
        let content_len = preamble.get_content_length();
        if !(content_len > 0
            && content_len < self.call_read_only_handler.maximum_call_argument_size)
        {
            return Err(Error::DecodeError(format!(
                "Invalid Http request: invalid body length for FastCallReadOnly ({})",
                content_len
            )));
        }
```

**File:** stackslib/src/net/api/fastcallreadonly.rs (L130-133)
```rust
        let parsed = parse_read_only_call_body(
            body,
            self.call_read_only_handler.read_only_call_max_mem_bytes,
        )?;
```

**File:** stackslib/src/net/api/read_only/parse.rs (L173-190)
```rust
/// Decode one hex-encoded Clarity value under the limiter. `error_msg` keeps
/// each endpoint's pre-existing error string.
fn deserialize_value(
    hex: &str,
    limiter: &ParseLimiter,
    error_msg: &'static str,
) -> Result<Value, Error> {
    let hex = hex.strip_prefix("0x").unwrap_or(hex);
    // The decoded size is known before allocating; reject early.
    limiter.preflight((hex.len() as u64).div_ceil(2))?;
    let value = {
        let data = hex_bytes(hex).map_err(|_e| Error::DecodeError(error_msg.into()))?;
        Value::deserialize_read(&mut data.as_slice(), None, false)
            .map_err(|_e| Error::DecodeError(error_msg.into()))?
    };
    limiter.checkpoint()?;
    Ok(value)
}
```

**File:** clarity-types/src/types/serialization.rs (L735-789)
```rust
                TypePrefix::List => {
                    let mut len = [0; 4];
                    r.read_exact(&mut len)?;
                    let len = u32::from_be_bytes(len);

                    if len > MAX_VALUE_SIZE {
                        return Err("Illegal list type".into());
                    }

                    let (list_type, _entry_type) = match expected_type {
                        None => (None, None),
                        Some(TypeSignature::SequenceType(SequenceSubtype::ListType(list_type))) => {
                            if len > list_type.get_max_len() {
                                // unwrap is safe because of the match condition
                                #[allow(clippy::unwrap_used)]
                                return Err(SerializationError::DeserializeExpected(Box::new(
                                    expected_type.cloned().unwrap(),
                                )));
                            }
                            (Some(list_type), Some(list_type.get_list_item_type()))
                        }
                        Some(x) => {
                            return Err(SerializationError::DeserializeExpected(Box::new(
                                x.clone(),
                            )));
                        }
                    };

                    if len > 0 {
                        let items = Vec::with_capacity(
                            (len as usize).min(INITIAL_DESERIALIZATION_CONTAINER_CAPACITY),
                        );
                        let stack_item = DeserializeStackItem::List {
                            items,
                            expected_len: len,
                            expected_type: list_type.cloned(),
                        };

                        stack.push(stack_item);
                        continue;
                    } else {
                        let finished_list = if let Some(list_type) = list_type {
                            Value::list_with_type(
                                &DESERIALIZATION_TYPE_CHECK_EPOCH,
                                vec![],
                                list_type.clone(),
                            )
                            .map_err(|_| "Illegal list type")?
                        } else {
                            Value::cons_list_unsanitized(vec![]).map_err(|_| "Illegal list type")?
                        };

                        Ok(finished_list)
                    }
                }
```

**File:** clarity-types/src/types/serialization.rs (L944-980)
```rust
                match stack_frame {
                    DeserializeStackItem::TopLevel { .. } => return Ok(item),
                    DeserializeStackItem::List {
                        items,
                        expected_len,
                        ..
                    } => {
                        items.push(item);
                        if (*expected_len as usize) <= items.len() {
                            // list is finished!
                            let Some(DeserializeStackItem::List {
                                items,
                                expected_type,
                                ..
                            }) = stack.pop()
                            else {
                                return Err(
                                    "BUG: deserializer stack should have a List frame on top"
                                        .into(),
                                );
                            };
                            let finished_list = if let Some(list_type) = expected_type {
                                Value::list_with_type(
                                    &DESERIALIZATION_TYPE_CHECK_EPOCH,
                                    items,
                                    list_type,
                                )
                                .map_err(|_| "Illegal list type")?
                            } else {
                                Value::cons_list_unsanitized(items)
                                    .map_err(|_| "Illegal list type")?
                            };

                            finished_item.replace(finished_list);
                        }
                        // else: not finished; keep the frame for the next element
                    }
```

**File:** stacks-common/src/alloc_tracker.rs (L94-123)
```rust
unsafe impl<A: GlobalAlloc> GlobalAlloc for TrackingAllocator<A> {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        let ptr = unsafe { self.inner.alloc(layout) };
        if !ptr.is_null() {
            let _ = THREAD_ALLOCATIONS.try_with(|c| {
                let next = c.get().increment(layout.size() as u64);
                c.set(next);
            });
        }
        ptr
    }

    unsafe fn dealloc(&self, ptr: *mut u8, layout: Layout) {
        unsafe { self.inner.dealloc(ptr, layout) };
        let _ = THREAD_ALLOCATIONS.try_with(|c| {
            let next = c.get().decrement(layout.size() as u64);
            c.set(next);
        });
    }

    unsafe fn alloc_zeroed(&self, layout: Layout) -> *mut u8 {
        let ptr = unsafe { self.inner.alloc_zeroed(layout) };
        if !ptr.is_null() {
            let _ = THREAD_ALLOCATIONS.try_with(|c| {
                let next = c.get().increment(layout.size() as u64);
                c.set(next);
            });
        }
        ptr
    }
```

**File:** clarity/src/vm/resource_limiter.rs (L151-178)
```rust
    /// Reject an upcoming allocation of `bytes` that would exceed the limit,
    /// so a known size can be refused before it is allocated.
    pub fn check_can_allocate(&self, bytes: u64) -> Result<(), String> {
        match self {
            NoTracking => Ok(()),
            MaxAllocated {
                baseline,
                limit_bytes,
            } => {
                let allocated = thread_allocated().net_allocated(baseline);
                if allocated.saturating_add(bytes) > *limit_bytes {
                    Err(format!(
                        "Net memory allocation of {allocated} bytes plus {bytes} upcoming bytes exceeds budget of {limit_bytes} bytes."
                    ))
                } else {
                    Ok(())
                }
            }
        }
    }

    /// Net bytes allocated since the baseline. `None` for `NoTracking`.
    pub fn net_allocated_bytes(&self) -> Option<u64> {
        match self {
            Self::NoTracking => None,
            Self::MaxAllocated { baseline, .. } => Some(thread_allocated().net_allocated(baseline)),
        }
    }
```

**File:** stacks-node/src/tests/mem_abort.rs (L290-337)
```rust
/// Execution is budgeted from the same total, so parsing must record what it
/// retained.
#[test]
fn test_read_only_parse_records_retained_mem() {
    let addr = SocketAddr::new(IpAddr::V4(Ipv4Addr::new(127, 0, 0, 1)), 33333);
    let conn_opts = ConnectionOptions::default();
    let mut http = StacksHttp::new(addr, &conn_opts);
    let request = new_call_read_request(addr, vec!["03".into(); 8]);
    let mut bytes = vec![];
    request.send(&mut bytes).unwrap();

    let (StacksHttpPreamble::Request(parsed_preamble), offset) =
        http.read_preamble(&bytes).unwrap()
    else {
        panic!("expected request preamble");
    };

    let mut handler = callreadonly::RPCCallReadOnlyRequestHandler::new(
        conn_opts.maximum_call_argument_size,
        BLOCK_LIMIT_MAINNET_21,
        Duration::from_secs(conn_opts.read_only_max_execution_time_secs),
        conn_opts.read_only_call_max_mem_bytes,
    );
    let path = format!(
        "/v2/contracts/call-read/{}/flood/f",
        StacksAddress::from_string("ST2DS4MSWSGJ3W9FBC6BVT0Y92S345HY8N3T6AV7R").unwrap()
    );
    let path_regex = handler.path_regex();
    let captures = path_regex.captures(&path).unwrap();
    handler
        .try_parse_request(&parsed_preamble, &captures, None, &bytes[offset..])
        .unwrap();

    assert!(handler.parse_retained_mem_bytes > 0);

    // with the limit disabled, retention is not measured.
    let mut handler = callreadonly::RPCCallReadOnlyRequestHandler::new(
        conn_opts.maximum_call_argument_size,
        BLOCK_LIMIT_MAINNET_21,
        Duration::from_secs(conn_opts.read_only_max_execution_time_secs),
        0,
    );
    handler
        .try_parse_request(&parsed_preamble, &captures, None, &bytes[offset..])
        .unwrap();

    assert_eq!(handler.parse_retained_mem_bytes, 0);
}
```

**File:** stackslib/src/net/api/callreadonly.rs (L130-163)
```rust
    fn try_parse_request(
        &mut self,
        preamble: &HttpRequestPreamble,
        captures: &Captures,
        query: Option<&str>,
        body: &[u8],
    ) -> Result<HttpRequestContents, Error> {
        let content_len = preamble.get_content_length();
        if !(content_len > 0 && content_len < self.maximum_call_argument_size) {
            return Err(Error::DecodeError(format!(
                "Invalid Http request: invalid body length for CallReadOnly ({})",
                content_len
            )));
        }

        if preamble.content_type != Some(HttpContentType::JSON) {
            return Err(Error::DecodeError(
                "Invalid content-type: expected application/json".to_string(),
            ));
        }

        let contract_identifier = request::get_contract_address(captures, "address", "contract")?;
        let function = request::get_clarity_name(captures, "function")?;
        let parsed = parse_read_only_call_body(body, self.read_only_call_max_mem_bytes)?;

        self.contract_identifier = Some(contract_identifier);
        self.function = Some(function);
        self.sender = Some(parsed.sender);
        self.sponsor = parsed.sponsor;
        self.arguments = Some(parsed.arguments);
        self.parse_retained_mem_bytes = parsed.retained_mem_bytes;

        Ok(HttpRequestContents::new().query_string(query))
    }
```
