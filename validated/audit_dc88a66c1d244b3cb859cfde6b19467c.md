### Title
`ArmadaGovernor`-style batch-split classification bypass analog in `pallet-multi-asset-bounties::propose_curator` — ([File: substrate/frame/multi-asset-bounties/src/lib.rs])

### Summary
`pallet-multi-asset-bounties` implements the exact aggregation guard the external report recommends — via `dispatch_context::with_context` and a `SpendContext` accumulator — inside `fund_bounty` [1](#0-0)  and inside `pallet-treasury::spend`/`spend_local` [2](#0-1) . However, `propose_curator`'s parent-bounty branch, which also authorizes a `SpendOrigin`-gated value against the same "maximum amount this origin is allowed to spend at a time" contract, performs only a bare per-call comparison and never enters the shared `SpendContext` accumulator.

### Finding Description
`Config::SpendOrigin` is documented as returning, via its `Success` value, "the maximum amount in a native asset that this origin is allowed to spend at a time" [3](#0-2) . `fund_bounty` correctly enforces this "at a time" semantics across an entire dispatch (including nested `utility::batch_all` calls) by tracking cumulative spend per `max_amount` bucket in `with_context::<SpendContext<T::Balance>, _>`, exactly mirroring the fix pattern recommended in the external report for the ArmadaGovernor bug: [4](#0-3) 

By contrast, `propose_curator`'s top-level-bounty branch (`child_bounty_id == None`) re-uses the identical `SpendOrigin`/`max_amount`/`native_amount` pattern but checks only the single call in isolation, with no `with_context` aggregation: [5](#0-4) 

This is the direct structural analog of `ArmadaGovernor::_classifyProposal`: a per-call threshold check exists to bound how much a single `SpendOrigin` authorization may approve "at a time," but when several `propose_curator` calls for different unassigned top-level bounties are bundled into one `utility::batch_all`/`batch` extrinsic, each call is evaluated against the *full* `max_amount` independently instead of a running total. A `SpendOrigin` intended to gate at most `max_amount` of native value per invocation can therefore re-authorize `N * max_amount` worth of bounty value in a single transaction, exactly as N sub-5% `distribute` calls in the report bypassed the aggregate 5% Extended-classification gate.

Existing guards do not stop this path because:
- The per-call `ensure!(native_amount <= max_amount, ...)` at line 795 has no visibility into sibling calls in the same batch.
- The `SpendContext` accumulator that would catch this (as it does for `fund_bounty`) is never invoked from `propose_curator`.
- `unassign_curator`/`propose_curator` is a normal signed-or-`SpendOrigin` extrinsic reachable from `pallet-utility`'s `batch`/`batch_all`/`force_batch`, requiring no privileged or governance actor beyond whatever `SpendOrigin` instance the runtime configures (e.g., a Council motion or track with a bounded `max_amount`).

### Impact Explanation
`propose_curator` transitions a bounty from `CuratorUnassigned` back to `Funded`, which is a prerequisite for `accept_curator` → `award_bounty` to release the bounty's already-escrowed value to a beneficiary chosen by the curator. By batching multiple `propose_curator` calls that each pass the per-call `max_amount` check, an entity holding a `SpendOrigin` scoped to authorize only one bounty's value "at a time" can re-activate the payout path for many bounties whose combined value exceeds that single-use authorization, extracting more value per extrinsic than the origin was designed to permit. This directly matches the "Balances… treasury spends… contract-held value must conserve value and settle exactly once to the rightful beneficiary and amount" impact category, since the amount ultimately released can exceed the intended per-authorization ceiling.

### Likelihood Explanation
The bug is reachable by any account holding (or colluding within) the configured `SpendOrigin` for top-level bounty re-curation, using only public, unprivileged extrinsics (`utility::batch_all` wrapping `propose_curator`). No malicious validator, collator, relayer, or admin is required — it is a pure runtime logic gap that is inconsistent with the pallet's own established mitigation pattern (`SpendContext`) used one function away in the same file.

### Recommendation
Wrap the `max_amount`/`native_amount` check in `propose_curator`'s `None` branch with the same `with_context::<SpendContext<T::Balance>, _>` accumulation used in `fund_bounty` and `pallet-treasury::spend`, so that all `SpendOrigin`-gated value approvals (funding and re-curation) share one running total per `max_amount` bucket for the lifetime of the outer dispatch/batch.

### Proof of Concept
1. Configure a runtime where `SpendOrigin` for `pallet-multi-asset-bounties` grants some account/track `max_amount = X` per authorization.
2. Create and fund two (or more) top-level bounties, each with value just under `X`, then have their curators unassigned via `unassign_curator` so they sit in `CuratorUnassigned` state.
3. Submit `utility::batch_all { calls: [propose_curator(bounty_1, curator), propose_curator(bounty_2, curator)] }` from the `SpendOrigin` account.
4. Both calls pass the standalone `ensure!(native_amount <= max_amount, ...)` check (line 795) because neither call sees the other's amount, whereas an equivalent `fund_bounty` batch exceeding `X` in aggregate would be rejected by the `SpendContext` guard (as demonstrated by the existing `fund_bounty_in_batch_respects_max_total` test) [6](#0-5) .
5. Both bounties transition to `Funded`; subsequent `accept_curator`/`award_bounty` calls release a combined value of `~2X`, exceeding what the `SpendOrigin`'s single `max_amount` was meant to authorize "at a time."

### Citations

**File:** substrate/frame/multi-asset-bounties/src/lib.rs (L285-287)
```rust
		/// The origin required for funding the bounty. The `Success` value is the maximum amount in
		/// a native asset that this origin is allowed to spend at a time.
		type SpendOrigin: EnsureOrigin<Self::RuntimeOrigin, Success = Self::Balance>;
```

**File:** substrate/frame/multi-asset-bounties/src/lib.rs (L593-613)
```rust
			let max_amount = T::SpendOrigin::ensure_origin(origin)?;
			let curator = T::Lookup::lookup(curator)?;
			ensure!(T::Preimages::len(&metadata).is_some(), Error::<T, I>::PreimageNotExist);

			let native_amount = T::BalanceConverter::from_asset_balance(value, *asset_kind.clone())
				.map_err(|_| Error::<T, I>::FailedToConvertBalance)?;
			ensure!(native_amount >= T::BountyValueMinimum::get(), Error::<T, I>::InvalidValue);
			ensure!(native_amount <= max_amount, Error::<T, I>::InsufficientPermission);

			with_context::<SpendContext<T::Balance>, _>(|v| {
				let context = v.or_default();
				let funding = context.spend_in_context.entry(max_amount).or_default();

				if funding.checked_add(&native_amount).map(|s| s > max_amount).unwrap_or(true) {
					Err(Error::<T, I>::InsufficientPermission)
				} else {
					*funding = funding.saturating_add(native_amount);
					Ok(())
				}
			})
			.unwrap_or(Ok(()))?;
```

**File:** substrate/frame/multi-asset-bounties/src/lib.rs (L788-796)
```rust
			match child_bounty_id {
				// Only `SpendOrigin` can propose curator for bounty
				None => {
					ensure!(maybe_sender.is_none(), BadOrigin);
					let max_amount = T::SpendOrigin::ensure_origin(origin)?;
					let native_amount = T::BalanceConverter::from_asset_balance(value, asset_kind)
						.map_err(|_| Error::<T, I>::FailedToConvertBalance)?;
					ensure!(native_amount <= max_amount, Error::<T, I>::InsufficientPermission);
				},
```

**File:** substrate/frame/treasury/src/lib.rs (L672-688)
```rust
			with_context::<SpendContext<BalanceOf<T, I>>, _>(|v| {
				let context = v.or_default();
				// We group based on `max_amount`, to distinguish between different kind of
				// origins. (assumes that all origins have different `max_amount`)
				//
				// Worst case is that we reject some "valid" request.
				let spend = context.spend_in_context.entry(max_amount).or_default();

				// Ensure that we don't overflow nor use more than `max_amount`
				if spend.checked_add(&native_amount).map(|s| s > max_amount).unwrap_or(true) {
					Err(Error::<T, I>::InsufficientPermission)
				} else {
					*spend = spend.saturating_add(native_amount);
					Ok(())
				}
			})
			.unwrap_or(Ok(()))?;
```

**File:** substrate/frame/multi-asset-bounties/src/tests.rs (L86-143)
```rust
#[test]
fn fund_bounty_in_batch_respects_max_total() {
	ExtBuilder::default().build_and_execute(|| {
		// Given
		let asset_kind = 1;
		let spend_origin = 10; // max spending of 10
		let value = 2; // `native_amount` is 2
		let curator = 4;
		let metadata = note_preimage(1);
		let _ = Balances::mint_into(&curator, Balances::minimum_balance());

		// When/Then
		// Respect the `max_total` for the given origin.
		assert_ok!(RuntimeCall::from(UtilityCall::batch_all {
			calls: vec![
				RuntimeCall::from(BountiesCall::fund_bounty {
					asset_kind: Box::new(asset_kind),
					value,
					curator,
					metadata
				}),
				RuntimeCall::from(BountiesCall::fund_bounty {
					asset_kind: Box::new(asset_kind),
					value,
					curator,
					metadata
				})
			]
		})
		.dispatch(RuntimeOrigin::signed(spend_origin)));

		// Given
		let value = 5; // `native_amount` is 5

		// When/Then
		// `spend` of 10 surpasses `max_total` for the given origin.
		assert_err_ignore_postinfo!(
			RuntimeCall::from(UtilityCall::batch_all {
				calls: vec![
					RuntimeCall::from(BountiesCall::fund_bounty {
						asset_kind: Box::new(asset_kind),
						value,
						curator,
						metadata
					}),
					RuntimeCall::from(BountiesCall::fund_bounty {
						asset_kind: Box::new(asset_kind),
						value,
						curator,
						metadata
					})
				]
			})
			.dispatch(RuntimeOrigin::signed(spend_origin)),
			Error::<Test>::InsufficientPermission
		);
	});
}
```
