Confirmed: `Filter` used by `eth_getLogs` in `substrate/frame/revive/rpc/src/lib.rs:20` is re-exported directly from the external `alloy_rpc_types` crate [1](#0-0) , and this crate's `FilterSet`/topics fields impose no bound on the number of addresses or topic hashes a client can submit in a single `eth_getLogs` JSON-RPC request. That filter is passed unchecked into `ReceiptProvider::logs`, where every address and every topic hash is expanded into a separate `push_bind` in a dynamically built SQL query before any weight/size limit is applied [2](#0-1) ; the only bound present, `MAX_LOG_RESULTS`, is a `LIMIT` applied to the *result* set, not to the *input* filter size [3](#0-2) .

This is a structurally identical bug-class to the CoreDAO report: an RPC-facing filter struct is deserialized from client-supplied JSON without an early bound on collection size, and that unbounded structure is walked/expanded during query construction — the CoreDAO fix was exactly to reject oversized topic lists during unmarshalling of `FilterCriteria`. Here, an unauthenticated RPC caller can submit `eth_getLogs` with an `address` array and/or up to 4 `topics` arrays each containing an extremely large number of entries (tens of thousands), causing the eth-rpc gateway to build and execute a SQL query with a correspondingly enormous number of bound parameters and `IN (...)` clauses across up to 5 fields, consuming excessive CPU/memory in the SQLite query planner and potentially exhausting the process's bind-parameter/parser resources per call, degrading or stalling the eth-rpc gateway service for that node.

Note this is scoped to the standalone `pallet-revive-eth-rpc` gateway process (an off-chain JSON-RPC server, not consensus-critical runtime code), so while it matches the report's bug class (missing early size validation on a filter struct before use), it is a service-availability issue in an auxiliary RPC component rather than a chain-halting or state-corrupting vulnerability. I could not find any equivalent unbounded-filter pattern in consensus-critical paths (message queue, XCMP, proof verification, staking/treasury payouts) — those all use `BoundedVec`/weight-metered decoding as shown by `MAX_TOPICS`-bounded `TopicFilter` in the statement store [4](#0-3)  and weight-checked XCMP message decoding [5](#0-4) .

### Title
Unbounded `eth_getLogs` filter (`address`/`topics`) causes unmetered SQL query expansion in pallet-revive-eth-rpc - (File: substrate/frame/revive/rpc/src/receipt_provider.rs)

### Summary
The `eth_getLogs` RPC handler accepts an `alloy_rpc_types::Filter` deserialized directly from client JSON with no cap on the number of `address` entries or `topics` hashes per position, then expands every entry into a bound SQL parameter when building the logs query.

### Finding Description
`EthRpcServer::get_logs` forwards the client-supplied `Filter` straight to `ReceiptProvider::logs` [6](#0-5) . Inside `logs`, the address list and each of the (up to 4) topic slots are iterated without any upper bound check, and every element becomes a separate `push_bind` in a dynamically constructed `QueryBuilder` [2](#0-1) . The only size guard in the function, `MAX_LOG_RESULTS`, is applied as an output `LIMIT`, not an input validation on the filter itself [3](#0-2) . Unlike the pallet-revive `logs` subscription path, which explicitly bounds addresses/topics via `BoundedOneOrMany<_, 1000>` and `BoundedVec<_, ConstU32<4>>` [7](#0-6) , the `eth_getLogs` request path has no analogous bound because `Filter` is an unmodified external type re-exported as-is [1](#0-0) .

### Impact Explanation
A single unauthenticated `eth_getLogs` call with a very large `address` array and/or large `topics` arrays forces the eth-rpc gateway to build and execute a SQL statement with a correspondingly huge number of bound parameters and `IN(...)` predicates, consuming disproportionate CPU/memory per request in the SQLite query builder/executor. Repeated requests can degrade or stall the RPC gateway process, denying log-query service to legitimate users of that node. This matches the "public underpriced work that degrades block production or stalls bridge processing" class in spirit, though scoped to the eth-rpc gateway service rather than consensus.

### Likelihood Explanation
Trivial to trigger: any client with network access to the JSON-RPC endpoint can submit an oversized `topics`/`address` filter with no authentication, no fee, and no prior on-chain action required.

### Recommendation
Validate `Filter.address` and each `Filter.topics` entry against a maximum cardinality (e.g., mirror the `ConstU32<4>`/`1000`-style bounds already used for the subscription filter type) immediately after deserializing the RPC parameter in `get_logs`/`EthRpcServer::get_logs`, rejecting the request with an `InvalidParams` error before it reaches `ReceiptProvider::logs`.

### Proof of Concept
Send a JSON-RPC request:
```json
{"jsonrpc":"2.0","method":"eth_getLogs","params":[{"topics":[["0x...","0x...", /* 100k unique 32-byte hashes */]]}],"id":1}
```
This is parsed into `Filter` with no size check [8](#0-7) , then `ReceiptProvider::logs` builds a SQL query binding all 100k hashes into a single `topic_0 IN (...)` clause [9](#0-8) , imposing large parse/bind/plan cost on the node's eth-rpc process for a single unauthenticated call.

### Citations

**File:** substrate/frame/revive/rpc/src/lib.rs (L20-20)
```rust
pub use alloy_rpc_types::{BlockId, BlockNumberOrTag, Filter, FilterBlockOption};
```

**File:** substrate/frame/revive/rpc/src/lib.rs (L464-467)
```rust
	async fn get_logs(&self, filter: Option<Filter>) -> RpcResult<FilterResults> {
		let logs = self.client.logs(filter).await?;
		Ok(FilterResults::Logs(logs))
	}
```

**File:** substrate/frame/revive/rpc/src/receipt_provider.rs (L776-797)
```rust
		if !filter.address.is_empty() {
			qb.push(" AND address IN (");
			let mut separated = qb.separated(", ");
			for addr in filter.address {
				separated.push_bind(addr.as_slice().to_vec());
			}
			separated.push_unseparated(")");
		}

		for (i, topic) in filter.topics.into_iter().enumerate() {
			if topic.is_empty() {
				continue;
			}

			qb.push(format_args!(" AND topic_{i} IN ("));
			let mut separated = qb.separated(", ");
			for hash in topic {
				separated.push_bind(hash.as_slice().to_vec());
			}
			separated.push_unseparated(")");
		}

```

**File:** substrate/frame/revive/rpc/src/receipt_provider.rs (L798-807)
```rust
		qb.push(" LIMIT ").push_bind(MAX_LOG_RESULTS as i64);

		let logs = qb.build().try_map(parse_log_row).fetch_all(&self.db_ctx.pool).await?;

		if logs.len() == MAX_LOG_RESULTS {
			log::warn!(
				target: LOG_TARGET,
				"Log query hit limit of {MAX_LOG_RESULTS}; results may be truncated",
			);
		}
```

**File:** substrate/primitives/statement-store/src/store_api.rs (L72-81)
```rust
pub enum TopicFilter {
	/// Matches all topics.
	Any,
	/// Matches only statements including all of the given topics.
	/// Bytes are expected to be a 32-byte topic. Up to [`MAX_TOPICS`] topics can be provided.
	MatchAll(BoundedVec<Topic, ConstU32<{ MAX_TOPICS as u32 }>>),
	/// Matches statements including any of the given topics.
	/// Bytes are expected to be a 32-byte topic. Up to [`MAX_ANY_TOPICS`] topics can be provided.
	MatchAny(BoundedVec<Topic, ConstU32<{ MAX_ANY_TOPICS as u32 }>>),
}
```

**File:** cumulus/pallets/xcmp-queue/src/lib.rs (L752-770)
```rust
	pub(crate) fn take_first_concatenated_xcm<'a>(
		data: &mut &'a [u8],
		meter: &mut WeightMeter,
	) -> Result<BoundedSlice<'a, u8, MaxXcmpMessageLenOf<T>>, ()> {
		// Let's make sure that we can decode at least an empty xcm message.
		let base_weight = T::WeightInfo::take_first_concatenated_xcm(0);
		if meter.try_consume(base_weight).is_err() {
			defensive!("Out of weight; could not decode all; dropping");
			return Err(());
		}

		let input_data = &mut &data[..];
		let mut input = codec::CountedInput::new(input_data);
		VersionedXcm::<()>::decode_with_depth_limit(MAX_XCM_DECODE_DEPTH, &mut input).map_err(
			|error| {
				tracing::debug!(target: LOG_TARGET, ?error, "Failed to decode XCM with depth limit");
				()
			},
		)?;
```

**File:** substrate/frame/revive/rpc/src/types/subscription.rs (L88-110)
```rust
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(untagged)]
pub enum SubscriptionOptions {
	/// Options passed when subscribing for logs.
	LogsOptions {
		/// An optional address to use to filter the logs.
		///
		/// If specified, then only logs where this address is the emitter will be returned in the
		/// subscription. If not specified, then it means that there's no filtering based on the
		/// address of the emitter.
		///
		/// If it's specified as a vector of addresses then all of the addresses specified in the
		/// vector pass the filter.
		#[serde(default, skip_serializing_if = "Option::is_none")]
		address: Option<BoundedOneOrMany<Address, 1000>>,

		/// An optional set of topics to filter the logs by.
		///
		/// If not specified, then logs with any topic would match the filter. If specified, then
		/// only logs which match the specified topics pass the filter.
		#[serde(default, skip_serializing_if = "Option::is_none")]
		topics: Option<BoundedVec<Option<BoundedOneOrMany<H256, 1000>>, ConstU32<4>>>,
	},
```

**File:** substrate/frame/revive/rpc/src/apis/execution_apis.rs (L102-104)
```rust
	/// Returns an array of all logs matching filter with given id.
	#[method(name = "eth_getLogs")]
	async fn get_logs(&self, filter: Option<Filter>) -> RpcResult<FilterResults>;
```
