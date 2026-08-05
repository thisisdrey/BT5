## Analysis

The Uniswap H-1 bug class is: **a bounded-budget callback is force-skipped (via a gas/weight ceiling check), yet the caller unconditionally commits the "callback consumed" state before/around that check, so the side effects the callback was supposed to perform are permanently lost with no retry path.**

The direct analog in this repository is `pallet-xcm`'s `OnResponse::on_response` implementation, which handles the notify-callback path for `report_outcome_notify` / `new_notify_query`.

### The corrupted value

In `polkadot/xcm/pallet-xcm/src/lib.rs`, the query record is deleted **before** the weight-budget check that decides whether the notify callback is dispatched at all: [1](#0-0) 

```rust
match maybe_notify {
    Some((pallet_index, call_index)) => {
        let bare = (pallet_index, call_index, query_id, response);
        if let Ok(call) = bare.using_encoded(|mut bytes| { ... }) {
            Queries::<T>::remove(query_id);
            let weight = call.get_dispatch_info().call_weight;
            if weight.any_gt(max_weight) {
                let e = Event::NotifyOverweight { ... };
                Self::deposit_event(e);
                return Weight::zero();
            }
            let dispatch_origin = Origin::Response(origin.clone()).into();
            match call.dispatch(dispatch_origin) { ... }
```

`Queries::<T>::remove(query_id)` runs unconditionally, then the code re-derives `weight = call.get_dispatch_info().call_weight` — the **declared/benchmarked** dispatch weight of the decoded notify call — and compares it against `max_weight`, the budget that was captured earlier at query-registration time via `report_outcome_notify`: [2](#0-1) 

The doc comment on `report_outcome_notify` itself flags this exact class of problem: the weight is *estimated* when the query is created and the actual weight is only known once the response arrives — "if it turns out to be heavier once it returns then reporting the outcome will fail." Just like the Solidity report's `unsubscribeGasLimit` being sized from a non-worst-case benchmark, `max_weight` here is a static/estimated figure computed at query-creation time (from a placeholder-args encoding of the call) that can diverge from the **actual** declared weight computed later from the real `(pallet_index, call_index, query_id, response)` tuple, whose size/shape depends on the concrete `Response` payload (e.g. `Response::Assets`/`Response::PalletsInfo`, whose encoded length and thus a parametrized `#[pallet::weight]` can vary).

### Why the guard does not stop the path

- `Queries::<T>::remove(query_id)` happens before the `weight.any_gt(max_weight)` check, so as soon as the response arrives, the query is **irrevocably consumed** — there is no re-queue/overweight-storage mechanism like the one `pallet-message-queue` provides for actually-overweight messages: [3](#0-2)  (`do_execute_overweight`, which *does* let a caller retry with a bigger budget — no equivalent exists for XCM notify queries).
- The only signal on the overweight path is the `NotifyOverweight` event; the query row is gone, so nothing can dispatch this callback later, ever.
- This is explicitly known/incomplete design, marked `// TODO #3735: Correct weight.` next to the analogous `SubscribeVersion` weight-zeroing path: [4](#0-3) .

### Impact

Any pallet or runtime logic that relies on `report_outcome_notify`/`new_notify_query` to complete a stateful action contingent on a remote outcome (e.g. finalizing a transfer, releasing a lock, completing a multi-leg settlement) can have that completion callback silently and permanently dropped if the actual dispatch weight of the decoded notify call (driven by the concrete `Response` content) exceeds the weight estimated at query-creation time — with the query already deleted, guaranteeing no retry. This matches "permanent user-fund or bridge-state lock" and "message queues/receipts/payout state advancing before dispatch/execution succeeds" from the impact gate.

### Caveat

Whether this is concretely triggerable end-to-end depends on the specific runtime's notify-callback call's weight function actually depending on `Response` payload size (a per-consumer property, not fixed in `pallet-xcm` itself), so exploitability is consumer-dependent rather than a guaranteed universal exploit in `pallet-xcm` alone. I could not find a concrete, currently-deployed consumer of `report_outcome_notify` in this repo whose notify call's weight is parametrized by `Response` content to fully prove an end-to-end unprivileged trigger — this would need to be validated per-runtime.

### Title
Notify query permanently deleted before weight-budget check causes silent, unretryable loss of settlement callback - (File: `polkadot/xcm/pallet-xcm/src/lib.rs`)

### Summary
`Pallet::on_response` removes the `Queries` storage entry for a pending notify query before checking whether the decoded notify call's actual dispatch weight exceeds the `max_weight` budget captured at query-registration time. If the real weight (dependent on the response payload) is even slightly higher than the estimate, the callback is skipped (`NotifyOverweight` event only) and the query can never be retried, since it no longer exists.

### Finding Description
`report_outcome_notify` computes `max_weight` from `notify.get_dispatch_info().call_weight` using a synthetically-built call at query-creation time [5](#0-4) . When the response actually arrives, `on_response` re-derives the call's weight from the real `(pallet_index, call_index, query_id, response)` tuple and only then compares it to `max_weight` — but only *after* unconditionally calling `Queries::<T>::remove(query_id)` [6](#0-5) . Any mismatch between the estimated and actual weight (e.g. due to response-size-dependent weight functions) results in the callback never executing while the query bookkeeping is already gone, unlike `pallet-message-queue` which preserves overweight messages for later manual execution.

### Impact Explanation
Any downstream logic gated on this notify callback (asset release, cross-chain settlement completion, etc.) can be permanently stranded with no way to re-trigger it, matching "permanent user-fund or bridge-state lock."

### Likelihood Explanation
Requires a legitimate responder (origin check still applies) to send back a `Response` whose encoded/weighed size differs enough from the estimate made at query creation to cross the `max_weight` threshold — likelihood is consumer/runtime-dependent, not a guaranteed universal trigger from `pallet-xcm` code alone.

### Recommendation
Do not remove the `Queries` entry until after the weight check succeeds and dispatch is attempted; on overweight, retain the query (or move it to a dedicated overweight queue, mirroring `pallet-message-queue::do_execute_overweight`) so it can be manually re-driven with a larger budget instead of being silently dropped.

### Proof of Concept
Not independently reproducible from `pallet-xcm` alone without a concrete consumer whose notify call's weight is parametrized by `Response` size; would require identifying such a runtime consumer to build an executable PoC.

### Citations

**File:** polkadot/xcm/pallet-xcm/src/lib.rs (L2981-2982)
```rust
		// TODO #3735: Correct weight.
		let instruction = SubscribeVersion { query_id, max_response_weight: Weight::zero() };
```

**File:** polkadot/xcm/pallet-xcm/src/lib.rs (L3474-3496)
```rust
	/// NOTE: `notify` gets called as part of handling an incoming message, so it should be
	/// lightweight. Its weight is estimated during this function and stored ready for
	/// weighing `ReportOutcome` on the way back. If it turns out to be heavier once it returns
	/// then reporting the outcome will fail. Furthermore if the estimate is too high, then it
	/// may be put in the overweight queue and need to be manually executed.
	pub fn report_outcome_notify(
		message: &mut Xcm<()>,
		responder: impl Into<Location>,
		notify: impl Into<<T as Config>::RuntimeCall>,
		timeout: BlockNumberFor<T>,
	) -> Result<(), XcmError> {
		let responder = responder.into();
		let destination = T::UniversalLocation::get().invert_target(&responder).map_err(|()| {
			tracing::debug!(
				target: "xcm::pallet_xcm::report_outcome_notify",
				"Failed to invert responder location to universal location",
			);
			XcmError::LocationNotInvertible
		})?;
		let notify: <T as Config>::RuntimeCall = notify.into();
		let max_weight = notify.get_dispatch_info().call_weight;
		let query_id = Self::new_notify_query(responder, notify, timeout, Here);
		let response_info = QueryResponseInfo { destination, query_id, max_weight };
```

**File:** polkadot/xcm/pallet-xcm/src/lib.rs (L4104-4126)
```rust
				match maybe_notify {
					Some((pallet_index, call_index)) => {
						// This is a bit horrible, but we happen to know that the `Call` will
						// be built by `(pallet_index: u8, call_index: u8, QueryId, Response)`.
						// So we just encode that and then re-encode to a real Call.
						let bare = (pallet_index, call_index, query_id, response);
						if let Ok(call) = bare.using_encoded(|mut bytes| {
							<T as Config>::RuntimeCall::decode(&mut bytes)
						}) {
							Queries::<T>::remove(query_id);
							let weight = call.get_dispatch_info().call_weight;
							if weight.any_gt(max_weight) {
								let e = Event::NotifyOverweight {
									query_id,
									pallet_index,
									call_index,
									actual_weight: weight,
									max_budgeted_weight: max_weight,
								};
								Self::deposit_event(e);
								return Weight::zero();
							}
							let dispatch_origin = Origin::Response(origin.clone()).into();
```

**File:** substrate/frame/message-queue/src/lib.rs (L1062-1078)
```rust
	/// Try to execute a single message that was marked as overweight.
	///
	/// The `weight_limit` is the weight that can be consumed to execute the message. The base
	/// weight of the function it self must be measured by the caller.
	pub fn do_execute_overweight(
		origin: MessageOriginOf<T>,
		page_index: PageIndex,
		index: T::Size,
		weight_limit: Weight,
	) -> Result<Weight, Error<T>> {
		match with_service_mutex(|| {
			Self::do_execute_overweight_inner(origin, page_index, index, weight_limit)
		}) {
			Err(()) => Err(Error::<T>::RecursiveDisallowed),
			Ok(x) => x,
		}
	}
```
