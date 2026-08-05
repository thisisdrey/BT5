### Title
`pallet-oracle`'s per-block anti-spam guard (`HasDispatched`) can be bypassed via the `DataFeeder::feed_value` trait entrypoint - (File: `substrate/frame/honzon/oracle/src/lib.rs`)

### Summary
The Malt `_notSameBlock()` bug is a case of a per-`msg.sender` guard that only exists on one call path (`bondToAccount`) and can be sidestepped by reaching the same effective logic through a different entrypoint (an intermediary contract), because the guard was bolted onto the outer function rather than the state-changing core. The same structural flaw exists in `pallet-oracle`: the "one submission per block" guard lives only in the `feed_values` extrinsic and is never enforced in `do_feed_values`, the function that actually performs the state-changing effect.

### Finding Description
`feed_values` is the only place that checks and updates `HasDispatched`: [1](#0-0) 

This storage item exists specifically to enforce "only one submission per oracle operator per block": [2](#0-1) 

However, the pallet exposes a second, independent public entrypoint — the `DataFeeder::feed_value` trait implementation — that calls the same underlying `do_feed_values` state-mutation function directly, **without ever touching `HasDispatched`**: [3](#0-2) 

`do_feed_values` is the actual state-changing core (writes `RawValues`, recomputes the aggregated `Values` via `CombineData::combine_data`, fires `OnNewData`): [4](#0-3) 

Exactly like the Solidity report — where `_notSameBlock()` was attached to `bondToAccount()` instead of the underlying bonding logic, so an attacker could reach the same logic through `attack2.forward()` and have a fresh `msg.sender` each time — here the guard is attached to the `feed_values` dispatchable instead of to `do_feed_values`. Any other pallet in the runtime that is wired to call `T::DataFeeder::feed_value(Some(who), key, value)` (the intended integration point documented in `substrate/frame/honzon/oracle/README.md`) reaches `do_feed_values` for the same `who` while completely skipping the `HasDispatched` insert/check. `ensure_account` only checks membership (`T::Members::contains(&who)`), not the per-block dispatch flag: [5](#0-4) 

### Impact Explanation
The corrupted invariant is "at most one raw price/value submission per oracle operator per block," which the pallet's own documentation states is required "to prevent spam and ensure fair participation." Because `feed_value` bypasses `HasDispatched`, an oracle operator whose account is reachable through both `feed_values` (extrinsic) and any runtime pallet wired to `DataFeeder::feed_value` can submit two (or more) raw values for the same key within one block. `do_feed_values` recomputes the median (`DefaultCombineData::combine_data`) after every single insertion: [6](#0-5) 

Because the median is recalculated using only the latest `RawValues` entry per operator (`RawValues` is keyed by `AccountId`, so a second write from the same operator simply overwrites the first), a single operator effectively gets a "redo" within the same block — allowing them to observe intermediate aggregate state and then push a second, more favorable value before the block closes, undermining the median's resistance to a single actor's influence within one block window. This directly compromises intended oracle-integrity behavior for any downstream consumer (e.g. lending/collateral pallets) that reads `Values`/`get()`.

### Likelihood Explanation
Exploitability depends on runtime wiring: it requires that some other pallet in the assembled runtime calls `T::DataFeeder::feed_value` with an operator-controlled `AccountId`. This is exactly the integration pattern the pallet's own docs recommend ("other pallets can use the `DataFeeder` trait" to feed values), so it is a realistic configuration rather than a contrived one. No governance action, leaked key, or malicious validator/collator is needed — only a pre-existing oracle operator (a normal, non-privileged-relative-to-governance role) using two legitimate call paths in the same block, mirroring the "custom smart contract" trick in the original report.

### Recommendation
Move the `HasDispatched` check/insert into `do_feed_values` (or have `feed_value`/`DataFeeder::feed_value` also perform the same try_insert against `HasDispatched`) so that the anti-spam invariant is enforced at the single state-mutating chokepoint rather than only in the `feed_values` extrinsic wrapper — analogous to moving `_notSameBlock()` into the core bonding logic instead of the wrapper function.

### Proof of Concept
1. Runtime `R` implements `pallet_other::Config` with `type OracleFeeder = pallet_oracle::Pallet<Runtime>` and exposes a dispatchable `pallet_other::submit(origin, key, value)` that internally calls `T::OracleFeeder::feed_value(Some(ensure_signed(origin)?), key, value)` (the pattern the oracle README explicitly recommends for pallet integration).
2. Oracle operator `Alice` (a `T::Members` member) at block `N`:
   - Calls `Oracle::feed_values(signed(Alice), [(K, 100)])` → succeeds, `HasDispatched` now contains `Alice`.
   - Calls `pallet_other::submit(signed(Alice), K, 999)` in the same block → reaches `Oracle::feed_value(Some(Alice), K, 999)` → `do_feed_values` executes and overwrites `RawValues[Alice][K]` and recomputes `Values[K]`, all without ever checking `HasDispatched`.
3. Result: Alice submitted twice for key `K` within block `N`, despite `feed_values`'s own guard nominally limiting her to one submission per block — confirmed by inspecting `RawValues::<T>::get(&Alice, &K)` reflecting the second value and the fact that `HasDispatched` was never consulted on the `feed_value` path (lines 462-473 vs. 367-373 in `substrate/frame/honzon/oracle/src/lib.rs`).

### Citations

**File:** substrate/frame/honzon/oracle/src/lib.rs (L285-295)
```rust
	/// A set of accounts that have already fed data in the current block.
	///
	/// This storage item tracks which oracle operators have already submitted data in the
	/// current block to enforce the "one submission per block" rule. This prevents spam and
	/// ensures fair participation among oracle operators.
	///
	/// The storage is cleared at the end of each block in the `on_finalize` hook, resetting
	/// the state for the next block.
	#[pallet::storage]
	pub(crate) type HasDispatched<T: Config<I>, I: 'static = ()> =
		StorageValue<_, BoundedBTreeSet<T::AccountId, T::MaxHasDispatchedSize>, ValueQuery>;
```

**File:** substrate/frame/honzon/oracle/src/lib.rs (L359-377)
```rust
		pub fn feed_values(
			origin: OriginFor<T>,
			values: BoundedVec<(T::OracleKey, T::OracleValue), T::MaxFeedValues>,
		) -> DispatchResultWithPostInfo {
			let feeder = ensure_signed_or_root(origin.clone())?;

			let who = Self::ensure_account(feeder)?;

			// ensure account hasn't dispatched an updated yet
			<HasDispatched<T, I>>::try_mutate(|set| {
				set.try_insert(who.clone())
					.map_err(|_| Error::<T, I>::ExceedsMaxHasDispatchedSize)?
					.then_some(())
					.ok_or(Error::<T, I>::AlreadyFeeded)
			})?;

			Self::do_feed_values(who, values.into());
			Ok(Pays::No.into())
		}
```

**File:** substrate/frame/honzon/oracle/src/lib.rs (L405-413)
```rust
	fn ensure_account(who: Option<T::AccountId>) -> Result<T::AccountId, DispatchError> {
		// ensure feeder is authorized
		if let Some(who) = who {
			ensure!(T::Members::contains(&who), Error::<T, I>::NoPermission);
			Ok(who)
		} else {
			Ok(Self::get_pallet_account())
		}
	}
```

**File:** substrate/frame/honzon/oracle/src/lib.rs (L415-429)
```rust
	fn do_feed_values(who: T::AccountId, values: Vec<(T::OracleKey, T::OracleValue)>) {
		let now = T::Time::now();
		for (key, value) in &values {
			let timestamped = TimestampedValue { value: value.clone(), timestamp: now };
			RawValues::<T, I>::insert(&who, key, timestamped);

			// Update `Values` storage if `combined` yielded result.
			if let Some(combined) = Self::combined(key) {
				<Values<T, I>>::insert(key, combined);
			}

			T::OnNewData::on_new_data(&who, key, value);
		}
		Self::deposit_event(Event::NewFeedData { sender: who, values });
	}
```

**File:** substrate/frame/honzon/oracle/src/lib.rs (L462-473)
```rust
impl<T: Config<I>, I: 'static> DataFeeder<T::OracleKey, T::OracleValue, T::AccountId>
	for Pallet<T, I>
{
	fn feed_value(
		who: Option<T::AccountId>,
		key: T::OracleKey,
		value: T::OracleValue,
	) -> DispatchResult {
		Self::do_feed_values(Self::ensure_account(who)?, vec![(key, value)]);
		Ok(())
	}
}
```

**File:** substrate/frame/honzon/oracle/src/default_combine_data.rs (L38-59)
```rust
	fn combine_data(
		_key: &<T as Config<I>>::OracleKey,
		mut values: Vec<TimestampedValueOf<T, I>>,
		prev_value: Option<TimestampedValueOf<T, I>>,
	) -> Option<TimestampedValueOf<T, I>> {
		let expires_in = ExpiresIn::get();
		let now = T::Time::now();

		values.retain(|x| x.timestamp.saturating_add(expires_in) > now);

		let count = values.len() as u32;
		let minimum_count = MinimumCount::get();
		if count < minimum_count || count == 0 {
			return prev_value;
		}

		let mid_index = count / 2;
		// Won't panic as `values` ensured not empty.
		let (_, value, _) =
			values.select_nth_unstable_by(mid_index as usize, |a, b| a.value.cmp(&b.value));
		Some(value.clone())
	}
```
