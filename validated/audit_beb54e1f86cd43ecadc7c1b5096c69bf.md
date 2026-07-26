### Title
Unbounded Response Materialization with No Response Size Cap in REST API Resource/Module Endpoints Enables Fullnode OOM - (`api/src/accounts.rs`, `api/src/context.rs`, `config/src/config/api_config.rs`)

---

### Summary

The Aptos REST API fully materializes all response data in memory before sending, with **no response size cap anywhere in the response path**. The `/accounts/{address}/resources` and `/accounts/{address}/modules` endpoints default to page sizes of **9999**, and resource groups are expanded post-fetch, potentially multiplying the actual item count. An unauthenticated client can trigger large memory allocations by requesting resources for accounts with many large resources, with no server-side budget enforcement until the OS OOM-kills the process.

---

### Finding Description

**Default page sizes are extremely large:**

`DEFAULT_MAX_ACCOUNT_RESOURCES_PAGE_SIZE = 9999` and `DEFAULT_MAX_ACCOUNT_MODULES_PAGE_SIZE = 9999` are set in `config/src/config/api_config.rs`. [1](#0-0) 

These are used as both the default and the ceiling in `determine_limit`: [2](#0-1) 

**Full materialization before sending — no response size cap:**

In `Account::resources()`, the server:
1. Fetches up to 9999 raw `(StructTag, Vec<u8>)` pairs from storage
2. **Expands resource groups** — each storage entry that is a resource group is deserialized and flattened into all its sub-resources, so the actual item count after expansion can exceed 9999
3. For JSON: converts every item from BCS bytes to a fully annotated `MoveResource` (type resolution, field name lookup, recursive struct annotation)
4. Collects the entire `Vec<MoveResource>` in memory
5. Calls `BasicResponse::try_from_json(...)` which wraps the entire `Vec` in a `Json<T>` payload — no size check [3](#0-2) [4](#0-3) 

The `try_from_json` path simply wraps the value in `poem_openapi::payload::Json(value)` and returns — there is no byte-budget check: [5](#0-4) 

**The only size guard is on the REQUEST body, not the response:**

`PostSizeLimit` (8 MB default) and `HeadersSanityCheck` only apply to POST request bodies: [6](#0-5) [7](#0-6) 

There is no analogous middleware or inline check on the response side.

**Same pattern in the modules endpoint:**

`Account::modules()` follows the identical pattern — fetches up to 9999 modules, parses each ABI (`try_parse_abi()`), collects into `Vec<MoveModuleBytecode>`, and calls `try_from_json` with no size cap: [8](#0-7) 

**Amplification via resource group expansion:**

The resource group expansion in `get_resources_by_pagination` takes `limit` storage entries, then flattens each resource group into all its sub-resources. A single storage entry that is a resource group containing N sub-resources counts as 1 against the limit but produces N items in the response. This is an unbounded multiplier: [9](#0-8) 

---

### Impact Explanation

An unauthenticated client sends a single GET request to `/v1/accounts/{address}/resources` (or `/modules`) for an account with many large resources. The fullnode:
- Fetches up to 9999 storage entries
- Expands resource groups (multiplying item count)
- Converts every item to a fully annotated JSON type (CPU + heap allocation per item)
- Materializes the entire `Vec<MoveResource>` in heap memory
- Serializes the entire collection to JSON (second large allocation)
- Only then begins sending — no size check at any point

If the aggregate allocation exceeds available memory, the OS OOM-killer terminates the fullnode process, denying service to all users. Even short of OOM, repeated concurrent requests can cause severe memory pressure and CPU thrash on the annotation/serialization path, degrading availability.

---

### Likelihood Explanation

- The attack is **unauthenticated** — any client can call the endpoint
- The `0x1` framework account on mainnet already has many resources; accounts with large resource groups (e.g., object stores, DeFi protocols) amplify the effect
- An attacker can create an account with many large resource groups (paying storage fees once) and then repeatedly call the API at zero marginal cost per request
- The default page size of 9999 means no `limit` parameter is needed — the default behavior is the worst case
- No concurrency gate exists on this endpoint (unlike `eth_call`/`eth_estimateGas` in the external report which had a permit-based limiter)

---

### Recommendation

1. **Add a response byte-budget middleware** that tracks bytes written and aborts with a 413/507 error once a configurable cap (e.g., 10 MB) is exceeded, analogous to how `PostSizeLimit` works for requests.
2. **Reduce default page sizes**: `DEFAULT_MAX_ACCOUNT_RESOURCES_PAGE_SIZE` and `DEFAULT_MAX_ACCOUNT_MODULES_PAGE_SIZE` of 9999 are far too large for a safe default; reduce to 100–500 and require explicit opt-in for larger pages.
3. **Cap post-expansion item count**: After resource group expansion in `get_resources_by_pagination`, enforce a hard cap on the total number of flattened items, not just the pre-expansion storage entries.
4. **Add a per-endpoint concurrency semaphore** for the resource/module listing endpoints to prevent concurrent large-response requests from multiplying memory pressure.

---

### Proof of Concept

```
# Single unauthenticated request — no body, no auth
GET /v1/accounts/0x1/resources
Accept: application/json
```

With default config (`max_account_resources_page_size = 9999`), the server fetches all resources for `0x1`, expands resource groups, annotates every field with Move type information, collects the full `Vec<MoveResource>` in heap, serializes to JSON, and sends — with no size check at any point.

To amplify: publish a module that writes a resource group with many large sub-resources, then call:

```
GET /v1/accounts/{attacker_address}/resources
Accept: application/json
```

Each call forces the server to re-materialize the full annotated JSON response from scratch, with no caching or size guard. [10](#0-9) [11](#0-10) [12](#0-11)

### Citations

**File:** config/src/config/api_config.rs (L97-102)
```rust
const DEFAULT_REQUEST_CONTENT_LENGTH_LIMIT: u64 = 8 * 1024 * 1024; // 8 MB
pub const DEFAULT_MAX_SUBMIT_TRANSACTION_BATCH_SIZE: usize = 10;
pub const DEFAULT_MAX_PAGE_SIZE: u16 = 100;
const DEFAULT_MAX_ACCOUNT_RESOURCES_PAGE_SIZE: u16 = 9999;
const DEFAULT_MAX_ACCOUNT_MODULES_PAGE_SIZE: u16 = 9999;
const DEFAULT_MAX_VIEW_GAS: u64 = 2_000_000; // We keep this value the same as the max number of gas allowed for one single transaction defined in aptos-gas.
```

**File:** api/src/accounts.rs (L85-128)
```rust
    #[oai(
        path = "/accounts/:address/resources",
        method = "get",
        operation_id = "get_account_resources",
        tag = "ApiTags::Accounts"
    )]
    async fn get_account_resources(
        &self,
        accept_type: AcceptType,
        /// Address of account with or without a `0x` prefix
        address: Path<Address>,
        /// Ledger version to get state of account
        ///
        /// If not provided, it will be the latest version
        ledger_version: Query<Option<U64>>,
        /// Cursor specifying where to start for pagination
        ///
        /// This cursor cannot be derived manually client-side. Instead, you must
        /// call this endpoint once without this query parameter specified, and
        /// then use the cursor returned in the X-Aptos-Cursor header in the
        /// response.
        start: Query<Option<StateKeyWrapper>>,
        /// Max number of account resources to retrieve
        ///
        /// If not provided, defaults to default page size.
        limit: Query<Option<u16>>,
    ) -> BasicResultWith404<Vec<MoveResource>> {
        fail_point_poem("endpoint_get_account_resources")?;
        self.context
            .check_api_output_enabled("Get account resources", &accept_type)?;

        let context = self.context.clone();
        api_spawn_blocking(move || {
            let account = Account::new(
                context,
                address.0,
                ledger_version.0,
                start.0.map(StateKey::from),
                limit.0,
            )?;
            account.resources(&accept_type)
        })
        .await
    }
```

**File:** api/src/accounts.rs (L448-462)
```rust
    pub fn resources(self, accept_type: &AcceptType) -> BasicResultWith404<Vec<MoveResource>> {
        let max_account_resources_page_size = self.context.max_account_resources_page_size();
        let (resources, next_state_key) = self
            .context
            .get_resources_by_pagination(
                self.address.into(),
                self.start.as_ref(),
                self.ledger_version,
                // Just use the max as the default
                determine_limit(
                    self.limit,
                    max_account_resources_page_size,
                    max_account_resources_page_size,
                    &self.latest_ledger_info,
                )? as u64,
```

**File:** api/src/accounts.rs (L473-496)
```rust
        match accept_type {
            AcceptType::Json => {
                // Resolve the BCS encoded versions into `MoveResource`s
                let state_view = self
                    .context
                    .latest_state_view_poem(&self.latest_ledger_info)?;
                let converter = state_view
                    .as_converter(self.context.db.clone(), self.context.indexer_reader.clone());
                let converted_resources = converter
                    .try_into_resources(resources.iter().map(|(k, v)| (k.clone(), v.as_slice())))
                    .context("Failed to build move resource response from data in DB")
                    .map_err(|err| {
                        BasicErrorWith404::internal_with_code(
                            err,
                            AptosErrorCode::InternalError,
                            &self.latest_ledger_info,
                        )
                    })?;
                BasicResponse::try_from_json((
                    converted_resources,
                    &self.latest_ledger_info,
                    BasicResponseStatus::Ok,
                ))
                .map(|v| v.with_cursor(next_state_key))
```

**File:** api/src/accounts.rs (L518-566)
```rust
    pub fn modules(self, accept_type: &AcceptType) -> BasicResultWith404<Vec<MoveModuleBytecode>> {
        let max_account_modules_page_size = self.context.max_account_modules_page_size();
        let (modules, next_state_key) = self
            .context
            .get_modules_by_pagination(
                self.address.into(),
                self.start.as_ref(),
                self.ledger_version,
                // Just use the max as the default
                determine_limit(
                    self.limit,
                    max_account_modules_page_size,
                    max_account_modules_page_size,
                    &self.latest_ledger_info,
                )? as u64,
            )
            .context("Failed to get modules from storage")
            .map_err(|err| {
                BasicErrorWith404::internal_with_code(
                    err,
                    AptosErrorCode::InternalError,
                    &self.latest_ledger_info,
                )
            })?;

        match accept_type {
            AcceptType::Json => {
                // Read bytecode and parse ABIs for output
                let mut converted_modules = Vec::new();
                for (_, module) in modules {
                    converted_modules.push(
                        MoveModuleBytecode::new(module.clone())
                            .try_parse_abi()
                            .context("Failed to parse move module ABI")
                            .map_err(|err| {
                                BasicErrorWith404::internal_with_code(
                                    err,
                                    AptosErrorCode::InternalError,
                                    &self.latest_ledger_info,
                                )
                            })?,
                    );
                }
                BasicResponse::try_from_json((
                    converted_modules,
                    &self.latest_ledger_info,
                    BasicResponseStatus::Ok,
                ))
                .map(|v| v.with_cursor(next_state_key))
```

**File:** api/src/context.rs (L460-537)
```rust
    pub fn get_resources_by_pagination(
        &self,
        address: AccountAddress,
        prev_state_key: Option<&StateKey>,
        version: u64,
        limit: u64,
    ) -> Result<(Vec<(StructTag, Vec<u8>)>, Option<StateKey>)> {
        let account_iter = self
            .indexer_reader
            .as_ref()
            .ok_or_else(|| format_err!("Indexer reader doesn't exist"))?
            .get_prefixed_state_value_iterator(
                &StateKeyPrefix::from(address),
                prev_state_key,
                version,
            )?;
        // TODO: Consider rewriting this to consider resource groups:
        // * If a resource group is found, expand
        // * Return Option<Result<(PathType, StructTag, Vec<u8>)>>
        // * Count resources and only include a resource group if it can completely fit
        // * Get next_key as the first struct_tag not included
        let mut resource_iter = account_iter
            .filter_map(|res| match res {
                Ok((k, v)) => match k.inner() {
                    StateKeyInner::AccessPath(AccessPath { address: _, path }) => {
                        match Path::try_from(path.as_slice()) {
                            Ok(Path::Resource(struct_tag)) => {
                                Some(Ok((struct_tag, v.bytes().to_vec())))
                            }
                            // TODO: Consider expanding to Path::Resource
                            Ok(Path::ResourceGroup(struct_tag)) => {
                                Some(Ok((struct_tag, v.bytes().to_vec())))
                            }
                            Ok(Path::Code(_)) => None,
                            Err(e) => Some(Err(anyhow::Error::from(e))),
                        }
                    }
                    _ => {
                        error!("storage prefix scan return inconsistent key ({:?}) with expected key prefix ({:?}).", k, StateKeyPrefix::from(address));
                        Some(Err(format_err!( "storage prefix scan return inconsistent key ({:?})", k )))
                    }
                },
                Err(e) => Some(Err(e)),
            })
            .take(limit as usize + 1);
        let kvs = resource_iter
            .by_ref()
            .take(limit as usize)
            .collect::<Result<Vec<(StructTag, Vec<u8>)>>>()?;

        // We should be able to do an unwrap here, otherwise the above db read would fail.
        let state_view = self.state_view_at_version(version)?;
        let converter = state_view.as_converter(self.db.clone(), self.indexer_reader.clone());

        // Extract resources from resource groups and flatten into all resources
        let kvs = kvs
            .into_iter()
            .map(|(tag, value)| {
                if converter.is_resource_group(&tag) {
                    // An error here means a storage invariant has been violated
                    bcs::from_bytes::<ResourceGroup>(&value)
                        .map(|map| map.into_iter().collect::<Vec<_>>())
                        .map_err(|e| e.into())
                } else {
                    Ok(vec![(tag, value)])
                }
            })
            .collect::<Result<Vec<Vec<(StructTag, Vec<u8>)>>>>()?
            .into_iter()
            .flatten()
            .collect();

        let next_key = if let Some((struct_tag, _v)) = resource_iter.next().transpose()? {
            Some(StateKey::resource(&address, &struct_tag)?)
        } else {
            None
        };
        Ok((kvs, next_key))
```

**File:** api/src/response.rs (L470-482)
```rust
           pub fn try_from_json<E: $crate::response::InternalError>(
                (value, ledger_info, status): (
                    T,
                    &aptos_api_types::LedgerInfo,
                    [<$enum_name Status>],
                ),
            ) -> Result<Self, E> {
               Ok(Self::from((
                    poem_openapi::payload::Json(value),
                    ledger_info,
                    status
               )))
            }
```

**File:** api/src/check_size.rs (L43-72)
```rust
impl<E: Endpoint> Endpoint for PostSizeLimitEndpoint<E> {
    type Output = E::Output;

    async fn call(&self, mut req: Request) -> Result<Self::Output> {
        // If the request method is not POST, skip the checks
        if req.method() != Method::POST {
            return self.inner.call(req).await;
        }

        // If Content-Length is present and exceeds the limit, reject the request
        if let Some(content_length) = req.headers().typed_get::<headers::ContentLength>() {
            if content_length.0 > self.max_size {
                return Err(SizedLimitError::PayloadTooLarge.into());
            }
        }

        // Verify the request body size is within the limit
        match req
            .take_body()
            .into_bytes_limit(self.max_size as usize)
            .await
        {
            Ok(bytes) => {
                req.set_body(Body::from_bytes(bytes));
                self.inner.call(req).await
            },
            Err(ReadBodyError::PayloadTooLarge) => Err(SizedLimitError::PayloadTooLarge.into()),
            Err(e) => Err(e.into()),
        }
    }
```

**File:** api/src/headers_sanity_check.rs (L42-70)
```rust
impl<E: Endpoint> Endpoint for HeadersSanityCheckEndpoint<E> {
    type Output = E::Output;

    async fn call(&self, req: Request) -> Result<Self::Output> {
        // If the request method is not POST, skip the checks
        if req.method() != Method::POST {
            return self.inner.call(req).await;
        }

        // If Content-Length is present and exceeds the limit, reject the request
        if let Some(content_length) = req.headers().typed_get::<headers::ContentLength>() {
            if content_length.0 > self.max_size {
                return Err(SizedLimitError::PayloadTooLarge.into());
            }
        }

        // Verify that Transfer-Encoding header is not set (as it is not supported)
        if req
            .headers()
            .typed_get::<headers::TransferEncoding>()
            .is_some()
        {
            return Err(BadRequest(std::io::Error::other(
                "Transfer-Encoding is not supported",
            )));
        }

        self.inner.call(req).await
    }
```
