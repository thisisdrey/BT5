### Title
Stuck Treasury XCM payouts: `PayOverXcm`/`TransferOverXcmHelper::check_transfer` reports `Pending` forever when the remote query never resolves, permanently locking the spend as unclaimable and unrecoverable - ([File: polkadot/xcm/xcm-builder/src/pay.rs] and [File: polkadot/xcm/xcm-builder/src/transfer.rs])

### Summary
`pallet-treasury`'s cross-chain payout path (`PayOverXcm` / `PayAccountId32OnChainOverXcm`) tracks the state of an approved spend purely through an XCM query created via `pallet-xcm`'s `Queries` storage. `check_payment`/`check_transfer` simply calls `Querier::take_response(id)` and maps the result: `Ready` → success/failure, `Pending` → `InProgress`, anything else → `Unknown`. There is no local mechanism to force-resolve, cancel, or re-issue a payout whose backing XCM query never receives a valid response (e.g. the remote chain never sends a matching `QueryResponse`, sends one with a mismatched `querier`/`responder`, or the destination silently drops the report). This mirrors the Etherfi bug class: an external system controls whether/when the pending request completes, and the local contract/pallet has no invalidation-handling path, so the debt (the earmarked spend) can be stuck indefinitely.

### Finding Description
`pallet-xcm::Queries` stores a `QueryStatus::Pending { responder, maybe_match_querier, maybe_notify, timeout }` entry when a query is created (`do_new_query`), [1](#0-0) . Resolution only happens through `OnResponse::on_response`, which requires the origin to exactly match `responder` and (if set) `maybe_match_querier` [2](#0-1) . If the response never arrives, or arrives from the "wrong" origin/querier, the events `InvalidResponder`/`InvalidQuerier`/`InvalidResponderVersion` are emitted and the query is explicitly left in `Pending` state for a possible future response [3](#0-2) .

`TransferOverXcmHelper::check_transfer` (used by `PayOverXcm`/`TransferOverXcm`) simply forwards this state: `Ready` maps to `Success`/`Failure`, `Pending` maps to `TransferStatus::InProgress`, and anything else (`NotFound`, `UnexpectedVersion`) maps to `Unknown` [4](#0-3) . `PayOverXcmWithHelper::check_payment` is a thin pass-through of the same result to `frame_support::traits::tokens::PaymentStatus` [5](#0-4) . The `Timeout` type parameter is only used to populate the `timeout` field of the query when it's created [6](#0-5) ; there is no code path in either `pallet-xcm` or `xcm-builder` that inspects this timeout and expires/removes the `Pending` entry once it elapses, nor any call that lets the payer void/re-trigger the spend once the deadline passes. Consequently a spend whose remote leg silently fails to report back (destination congestion, version mismatch, remote-side filtering of the `ReportError`/`SetAppendix` instruction, remote reaping the appendix due to insufficient weight, etc.) remains `PaymentStatus::InProgress` forever from `pallet-treasury`'s point of view.

Because `pallet-treasury`'s spend lifecycle (`Spend`/`SpendStatus` machinery, gated by `check_status`) relies on `Pay::check_payment` transitioning to a terminal state (`Success`/`Failure`) to let the spend be finalized, retried, or reclaimed, a payout that is stuck in `Pending` can never be settled: it cannot be marked failed and refunded to the treasury pot, and it cannot be confirmed as delivered. This is the direct analog of the WithdrawRequestNFT case: the "local ledger" (Queries storage / spend accounting) depends on an external actor (the remote chain / relayer set) actually delivering a well-formed response, and there is no fallback path to reconcile the state if that never happens.

### Impact Explanation
Any runtime that wires `pallet-treasury` (or any other pallet using `frame_support::traits::tokens::Pay`) to `PayOverXcm`/`PayAccountId32OnChainOverXcm` is exposed: a treasury spend whose remote-chain leg never round-trips a matching `QueryResponse` becomes permanently un-resolvable in local state. This is a "permanent user-fund or bridge-state lock" class issue per the impact gate — the earmarked amount is neither returned to the pot nor delivered to the beneficiary, and the spend record occupies storage indefinitely with no permissionless or root path to force resolution once the timeout elapses.

### Likelihood Explanation
This does not require a malicious relayer, validator, or governance actor — it can occur through ordinary cross-chain unreliability: version incompatibility causing `InvalidResponderVersion`/`InvalidQuerierVersion` [7](#0-6) , a remote chain that fails to execute the `ReportError`/`SetAppendix(ReportError(...))` instruction (e.g. insufficient remote weight or barrier rejection) as built in `remote_transfer_xcm_free_execution` [8](#0-7) , or normal network/channel congestion causing the response message to be dropped. Because the `Timeout` parameter is never enforced anywhere in the query-resolution code, the likelihood of hitting an unrecoverable `Pending` state over the life of a chain using cross-chain treasury payouts is non-trivial, matching the "acknowledged, no automatic recovery, needs manual/governance handling" profile of the source report.

### Recommendation
- Enforce the `timeout` stored in `QueryStatus::Pending` inside `pallet-xcm`: add a mechanism (e.g. a scheduled hook or a permissionless `expire_query`/`expect_response`-style extrinsic) that transitions an expired `Pending` query into a terminal `Ready`/failed state once `timeout` has passed without a matching response.
- Propagate this terminal "expired" state through `TransferOverXcmHelperT::check_transfer` / `PayOverXcmWithHelper::check_payment` so that `frame_support::traits::tokens::Pay` consumers (like `pallet-treasury`) see a definite `Failure` rather than an indefinite `InProgress`.
- Add a treasury-side (or generic `Pay`-consumer-side) fallback that, once a payment id has passed its expected timeout and is still not resolved, allows the spend to be voided and the funds returned to the pot (or retried), instead of relying purely on the remote system to eventually respond correctly.

### Proof of Concept
1. Configure a runtime with `pallet-treasury::Config::Paymaster = PayAccountId32OnChainOverXcm<...>` (or `PayOverXcm`) targeting a remote chain, with some `Timeout::get()` value `T`.
2. Approve and initiate a spend; `TransferOverXcmHelper::send_remote_transfer_xcm` creates a `pallet-xcm` query via `Querier::new_query(asset_location, Timeout::get(), from_location.interior)`, inserting `Queries::<T>::insert(id, QueryStatus::Pending { .. , timeout: T })` [6](#0-5) .
3. Simulate the remote chain either dropping the `ReportError` appendix (e.g., insufficient remote execution weight/`Barrier` rejection) or sending a `QueryResponse` from a location that fails the `querier`/`responder` match in `on_response`, so `InvalidResponder`/`InvalidQuerier`/`InvalidResponderVersion` fires and the entry stays `QueryStatus::Pending` [7](#0-6) .
4. Advance the chain well past block `T` (the stored `timeout`). Call `PayOverXcmWithHelper::check_payment(id)` (equivalently `Pools::check_status` inside treasury's payout flow) — observe it still returns `PaymentStatus::InProgress` because `check_transfer` only inspects `Querier::take_response`, which still returns `Pending` since nothing ever transitions the query out of `Pending` on timeout [4](#0-3) .
5. There is no extrinsic or hook in `pallet-xcm` or `pallet-treasury` in this codebase that force-expires the query or voids the spend after `timeout`, so the spend remains permanently unresolved — funds are neither released to the beneficiary nor returned to the treasury pot.

### Citations

**File:** polkadot/xcm/pallet-xcm/src/lib.rs (L558-573)
```rust
		/// Expected query response has been received but the origin location of the response does
		/// not match that expected. The query remains registered for a later, valid, response to
		/// be received and acted upon.
		InvalidResponder {
			origin: Location,
			query_id: QueryId,
			expected_location: Option<Location>,
		},
		/// Expected query response has been received but the expected origin location placed in
		/// storage by this runtime previously cannot be decoded. The query remains registered.
		///
		/// This is unexpected (since a location placed in storage in a previously executing
		/// runtime should be readable prior to query timeout) and dangerous since the possibly
		/// valid response will be dropped. Manual governance intervention is probably going to be
		/// needed.
		InvalidResponderVersion { origin: Location, query_id: QueryId },
```

**File:** polkadot/xcm/pallet-xcm/src/lib.rs (L3441-3455)
```rust
		QueryCounter::<T>::mutate(|q| {
			let r = *q;
			q.saturating_inc();
			Queries::<T>::insert(
				r,
				QueryStatus::Pending {
					responder: responder.into().into(),
					maybe_match_querier: Some(match_querier.into().into()),
					maybe_notify,
					timeout,
				},
			);
			r
		})
	}
```

**File:** polkadot/xcm/pallet-xcm/src/lib.rs (L4086-4104)
```rust
				let responder = match Location::try_from(responder) {
					Ok(r) => r,
					Err(_) => {
						Self::deposit_event(Event::InvalidResponderVersion {
							origin: origin.clone(),
							query_id,
						});
						return Weight::zero();
					},
				};
				if origin != responder {
					Self::deposit_event(Event::InvalidResponder {
						origin: origin.clone(),
						query_id,
						expected_location: Some(responder),
					});
					return Weight::zero();
				}
				match maybe_notify {
```

**File:** polkadot/xcm/xcm-builder/src/transfer.rs (L206-210)
```rust
		let query_id = Querier::new_query(
			asset_location.clone(),
			Timeout::get(),
			from_location.interior.clone(),
		);
```

**File:** polkadot/xcm/xcm-builder/src/transfer.rs (L239-250)
```rust
	fn check_transfer(id: Self::QueryId) -> TransferStatus {
		use QueryResponseStatus::*;
		match Querier::take_response(id) {
			Ready { response, .. } => match response {
				Response::ExecutionResult(None) => TransferStatus::Success,
				Response::ExecutionResult(Some(_)) => TransferStatus::Failure,
				_ => TransferStatus::Unknown,
			},
			Pending { .. } => TransferStatus::InProgress,
			NotFound | UnexpectedVersion => TransferStatus::Unknown,
		}
	}
```

**File:** polkadot/xcm/xcm-builder/src/transfer.rs (L359-374)
```rust
	let xcm = Xcm(vec![
		DescendOrigin(from_location.interior),
		UnpaidExecution { weight_limit: Unlimited, check_origin: None },
		SetAppendix(Xcm(vec![
			SetFeesMode { jit_withdraw: true },
			ReportError(QueryResponseInfo {
				destination: origin_relative_to_remote,
				query_id,
				max_weight: Weight::zero(),
			}),
		])),
		TransferAsset {
			beneficiary,
			assets: vec![Asset { id: asset_id, fun: Fungibility::Fungible(amount) }].into(),
		},
	]);
```

**File:** polkadot/xcm/xcm-builder/src/pay.rs (L108-110)
```rust
	fn check_payment(id: Self::Id) -> PaymentStatus {
		TransferOverXcmHelper::check_transfer(id)
	}
```
