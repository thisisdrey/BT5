### Title
`pallet-oracle::feed_values` applies price updates synchronously with no per-price staleness/replay gap, letting an oracle feeder atomically batch a price update with a downstream trade — ([File: substrate/frame/honzon/oracle/src/lib.rs])

### Summary
`pallet-oracle` (the new Acala-derived oracle introduced for the AssetHub stablecoin project, `pallet-oracle`) implements exactly the "pull-model" pattern described in the Stork/Pyth report: a permitted feeder calls `feed_values`, the pallet immediately recomputes the aggregated `Values` entry in the *same* extrinsic (`do_feed_values` → `Self::combined(key)` → `<Values<T, I>>::insert`), and that freshly aggregated price is instantly visible to any other pallet consuming `DataProvider::get`/`DataProviderExtended::get_all_values`. The only anti-spam control is "one feed per account per block" (`HasDispatched`); there is no minimum time-since-last-update, no staleness bound on the *aggregate* price, and no restriction preventing the feed call and a price-consuming call (e.g. a loan/CDP open+liquidate, a swap, a stablecoin mint/redeem) from being batched into one atomic transaction via `pallet_utility::batch_all`. This reproduces the exact primitive from the external report: a permissioned-but-not-fully-trusted actor (oracle operator, analogous to Stork/Pyth's "executor") can set a favorable price and consume it for settlement all within a single atomic transaction, and can repeat this every block.

### Finding Description
`feed_values` in `substrate/frame/honzon/oracle/src/lib.rs` is guarded only by `T::Members::contains(&who)` (or root) and by `HasDispatched` (once per account per block): [1](#0-0) 

The actual state transition happens in `do_feed_values`, which stores the raw value and — critically — immediately recomputes and overwrites the aggregate `Values` entry in the same call, with no delay, no minimum-price-age check, and no bound requiring the new timestamp to represent a meaningfully later block than the previous aggregate: [2](#0-1) 

`combined()` recomputes the median purely from whatever is currently in `RawValues` (a mix of possibly-stale entries from other members and the just-submitted fresh one): [3](#0-2) 

Any other pallet built on top of `DataProvider`/`DataProviderExtended` (the stablecoin/loan pallet this was introduced for, per PR "Polkadot Stablecoin on AssetHub") reads this aggregate directly via `Pallet::<T,I>::get`, with the update and the consuming logic composable in the same transaction using `pallet_utility::batch_all`: [4](#0-3) 

The pallet's own documentation confirms the design intent that "Only one submission per oracle operator per block is allowed to prevent spam" — which mirrors precisely the report's recommendation ("limit price updates to once per block") but does **not** address the actual arbitrage primitive: an operator can still (a) submit a self-favorable value that immediately becomes (or nudges) the aggregate, (b) in the same atomic transaction, use that updated aggregate to open/close a position, mint/redeem, or trigger a swap priced off the oracle, and (c) repeat next block. There is no enforcement that the *consumed* value be older than N blocks, nor that price-affecting extrinsics cannot be dispatched in the same transaction as `feed_values` (no `SignedExtension`/filter prevents batching `Oracle::feed_values` with another pallet's call via `Utility::batch_all`).

### Impact Explanation
Any downstream pallet that prices collateral, mints/burns a stablecoin, or executes swaps/liquidations against `pallet-oracle`'s aggregated `Values` inherits this exact arbitrage window: an oracle operator (or, per the pallet's own root-operator design, the `RootOperatorAccountId`) can manufacture a favorable price and consume it atomically, extracting value from the protocol/other users before the price reverts to a fair market value in a subsequent block. This falls squarely under "theft or unbacked mint" and "runtime bugs that compromise intended behavior" for the stablecoin/lending logic this oracle is meant to secure.

### Likelihood Explanation
Medium: the attacker must be a member of `T::Members` (an oracle feeder) or hold the root-operator account — a permissioned-but-not-fully-privileged role (analogous to Stork/Pyth's "executor," not chain governance/sudo). Given the pallet is explicitly designed to support "a configurable set of oracle operators," and multiple independent operators may exist, any single dishonest or compromised operator can execute the attack unilaterally and repeatedly (once per block, indefinitely), without needing to control consensus, be a validator/collator, or obtain any additional privilege.

### Recommendation
- Enforce a minimum age/staleness window before a freshly submitted price can be consumed by downstream pricing logic (e.g., require `now - timestamp >= MinPriceAge` before use, not just before storage).
- Decouple `feed_values` from any pricing-consumption call within the same transaction — e.g., disallow `Oracle::feed_values` inside `Utility::batch`/`batch_all`, or require consuming pallets to snapshot the price from *the previous block* rather than the just-updated current-block value.
- Consider TWAP-style aggregation or bounding maximum single-block price movement so that one operator's submission cannot unilaterally swing the consumed aggregate enough to be profitably arbitraged.

### Proof of Concept
1. Attacker Alice is a member of `T::Members` for `pallet-oracle`, and the runtime wires `DataProvider::get` from this pallet into a downstream lending/stablecoin pallet (per PR #9765's intended usage).
2. Alice constructs a single `Utility::batch_all` transaction containing:
   - `Oracle::feed_values([(collateral_key, favorable_price)])` — passes `T::Members::contains` and `HasDispatched` checks, and immediately updates `Values` via `do_feed_values`/`combined` (`substrate/frame/honzon/oracle/src/lib.rs:415-429`).
   - A call into the downstream pallet that reads `Oracle::get(collateral_key)` to price a loan draw-down / swap / redemption at the just-set favorable price.
   - A final call reverting/closing the position (e.g. repay/withdraw) before the price is corrected by other operators in a later block.
3. Because all three calls execute atomically in one block/transaction, and `Values` is updated synchronously with no staleness gate, Alice extracts the price differential as profit, and can repeat the same batch every subsequent block (limited only by `HasDispatched`'s once-per-block-per-account rule, which does not prevent the attack, only its frequency). [5](#0-4)

### Citations

**File:** substrate/frame/honzon/oracle/src/lib.rs (L359-429)
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
	}
}

impl<T: Config<I>, I: 'static> Pallet<T, I> {
	fn get_pallet_account() -> T::AccountId {
		T::PalletId::get().into_account_truncating()
	}

	/// Reads the raw values for a given key from all oracle members.
	pub fn read_raw_values(key: &T::OracleKey) -> Vec<TimestampedValueOf<T, I>> {
		T::Members::sorted_members()
			.iter()
			.chain([Self::get_pallet_account()].iter())
			.filter_map(|x| Self::raw_values(x, key))
			.collect()
	}

	/// Returns the aggregated and timestamped value for a given key.
	pub fn get(key: &T::OracleKey) -> Option<TimestampedValueOf<T, I>> {
		Self::values(key)
	}

	fn combined(key: &T::OracleKey) -> Option<TimestampedValueOf<T, I>> {
		let values = Self::read_raw_values(key);
		T::CombineData::combine_data(key, values, Self::values(key))
	}

	fn ensure_account(who: Option<T::AccountId>) -> Result<T::AccountId, DispatchError> {
		// ensure feeder is authorized
		if let Some(who) = who {
			ensure!(T::Members::contains(&who), Error::<T, I>::NoPermission);
			Ok(who)
		} else {
			Ok(Self::get_pallet_account())
		}
	}

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

**File:** substrate/frame/honzon/oracle/src/lib.rs (L449-453)
```rust
impl<T: Config<I>, I: 'static> DataProvider<T::OracleKey, T::OracleValue> for Pallet<T, I> {
	fn get(key: &T::OracleKey) -> Option<T::OracleValue> {
		Self::get(key).map(|timestamped_value| timestamped_value.value)
	}
}
```
