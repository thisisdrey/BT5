### Title
Lottery winner is predictable/manipulable because `buy_ticket` stays open after the epoch randomness used for the draw is already fixed and public - ([File: substrate/frame/lottery/src/lib.rs])

### Summary
`pallet_lottery::do_buy_ticket` accepts unlimited, permissionless ticket purchases from any signed account up until `block_number < config.start + config.length` [1](#0-0) , and the winner is chosen later via `T::Randomness::random(...)` combined with a naive `random_number % total` selection over the *final* ticket count [2](#0-1) . The pallet's configured `Randomness` source (e.g. `pallet_babe::RandomnessFromOneEpochAgo`, the type this codebase explicitly recommends for exactly this kind of "auction/lottery ending" logic) publishes its value at the *start* of the epoch it is used in, well before that epoch's blocks — including the lottery's payout block — are produced [3](#0-2) . This is the direct structural analog of the `Pod.depositTo()` bug: a state-changing, unprivileged, permissionless deposit-like action (`buy_ticket`) remains open after the "prize outcome" (the random seed) is already knowable on-chain, letting an attacker act on privileged foreknowledge to seize the payout, exactly as `pauseDepositsDuringAwarding` was meant to prevent for `Pod`.

### Finding Description
- `start_lottery` lets the `ManagerOrigin` configure `price`, `length`, and `delay` with no lower bound tying `delay` to epoch boundaries or randomness freshness [4](#0-3) .
- Ticket purchases (`buy_ticket` → `do_buy_ticket`) are open to any signed account and are only cut off at `config.start + config.length`, i.e. strictly *before* the payout block at `config.start + config.length + config.delay` [5](#0-4) [6](#0-5) .
- At payout time, `on_initialize` calls `Self::choose_account()` → `choose_ticket(TicketsCount)` → `Self::generate_random_number(seed)` → `T::Randomness::random(...)`, then simply does `random_number % total` [7](#0-6) [8](#0-7) .
- The pallet's own code documents that this randomness has known "freshness" problems (`TODO: deal with randomness freshness`) [9](#0-8) , and the BABE randomness sources it is meant to be paired with (`RandomnessFromOneEpochAgo`) are explicitly documented as being learned "at the end of the previous epoch," i.e. fully fixed and publicly readable in on-chain storage before the epoch that consumes it (and hence before the lottery's payout block) even starts [10](#0-9) .
- Because `total` (`TicketsCount`) is still mutable via `do_buy_ticket` right up to `config.start + config.length`, and the seed feeding `random_number` can already be public by that time, any unprivileged account can: (1) read the already-committed randomness value, (2) compute what index `random_number % total` will select for every candidate value of `total`, and (3) submit exactly the ticket purchase(s) needed (buying one or more of the allowed distinct "valid calls") to make `total` land on a value where the resulting index belongs to their own ticket(s), guaranteeing they win the full pot contributed by all other participants.
- No guard analogous to `pauseDepositsDuringAwarding` exists to stop `buy_ticket` once the randomness relevant to the current draw is already fixed/known.

### Impact Explanation
This lets an unprivileged, non-admin, non-validator attacker deterministically steal the entire lottery pot funded by other honest participants' deposits, which is a direct value-theft / wrong-beneficiary outcome (the pot goes to an attacker who contributed the minimum necessary rather than being fairly distributed by chance). Any production runtime enabling `pallet_lottery` (the pallet is wired into `substrate/bin/node/runtime/src/lib.rs`) inherits this exposure.

### Likelihood Explanation
The attack requires only: a signed account able to submit extrinsics, public on-chain state reads (randomness storage and `TicketsCount`), and precise timing of ordinary `buy_ticket` calls before `config.start + config.length`. No malicious validator, collator, relayer, or admin/governance action is needed — the manager only needs to run an ordinary lottery with typical `delay` values; the randomness source's inherent "learned at epoch start" property does the rest. This is a purely public-entrypoint, permissionless manipulation, matching the exploited primitive in the original report (acting with foreknowledge of an outcome before the deposit window closes).

### Recommendation
- Enforce that `buy_ticket` closes strictly before the epoch (or randomness-generation boundary) that will supply the seed used in `choose_ticket` — i.e., require `delay` (and the epoch length of the configured `Randomness` source) to guarantee the seed is *not yet fixed* when the ticket-selling window is still open, analogous to adding a `pauseDepositsDuringAwarding`-style check to `do_buy_ticket`.
- Alternatively, commit to the total number of tickets (or a hash of it) before the randomness is revealed, and derive the winner using a scheme that cannot be biased by post-hoc ticket purchases (e.g., VRF-based commit/reveal specific to this draw, not a globally-shared epoch-level randomness value).
- Add regression tests that simulate an attacker observing the fixed randomness value and buying a ticket in the final block of the selling window to confirm they cannot force the winning slot.

### Proof of Concept
1. Manager calls `start_lottery(price, length, delay, repeat)`.
2. As the epoch containing block `start + length + delay` begins, the relevant `NextRandomness`/epoch randomness (feeding `T::Randomness::random`) becomes fixed and is readable from chain state [11](#0-10) .
3. Attacker computes `random_number = H(seed ++ randomness)` for `seed = 0` (and subsequent seeds per `MaxGenerateRandom`) exactly as `generate_random_number` does [12](#0-11) .
4. While `block_number < start + length` is still true, attacker submits `buy_ticket` transactions to push `TicketsCount` to a value `total` for which `random_number % total` equals an index they hold (their own `Tickets::<T>` slot), guaranteeing selection in `choose_ticket`/`choose_account` [2](#0-1) .
5. At `on_initialize`, the pot (funded by all other participants' ticket purchases) is transferred entirely to the attacker [13](#0-12) .

### Citations

**File:** substrate/frame/lottery/src/lib.rs (L243-262)
```rust
		fn on_initialize(n: BlockNumberFor<T>) -> Weight {
			Lottery::<T>::mutate(|mut lottery| -> Weight {
				if let Some(config) = &mut lottery {
					let payout_block =
						config.start.saturating_add(config.length).saturating_add(config.delay);
					if payout_block <= n {
						let (lottery_account, lottery_balance) = Self::pot();

						let winner = Self::choose_account().unwrap_or(lottery_account);
						// Not much we can do if this fails...
						let res = T::Currency::transfer(
							&Self::account_id(),
							&winner,
							lottery_balance,
							KeepAlive,
						);
						debug_assert!(res.is_ok());

						Self::deposit_event(Event::<T>::Winner { winner, lottery_balance });

```

**File:** substrate/frame/lottery/src/lib.rs (L304-313)
```rust
		pub fn buy_ticket(
			origin: OriginFor<T>,
			call: Box<<T as Config>::RuntimeCall>,
		) -> DispatchResult {
			let caller = ensure_signed(origin.clone())?;
			call.clone().dispatch(origin).map_err(|e| e.error)?;

			let _ = Self::do_buy_ticket(&caller, &call);
			Ok(())
		}
```

**File:** substrate/frame/lottery/src/lib.rs (L339-377)
```rust
		/// Start a lottery using the provided configuration.
		///
		/// This extrinsic must be called by the `ManagerOrigin`.
		///
		/// Parameters:
		///
		/// * `price`: The cost of a single ticket.
		/// * `length`: How long the lottery should run for starting at the current block.
		/// * `delay`: How long after the lottery end we should wait before picking a winner.
		/// * `repeat`: If the lottery should repeat when completed.
		#[pallet::call_index(2)]
		#[pallet::weight(T::WeightInfo::start_lottery())]
		pub fn start_lottery(
			origin: OriginFor<T>,
			price: BalanceOf<T>,
			length: BlockNumberFor<T>,
			delay: BlockNumberFor<T>,
			repeat: bool,
		) -> DispatchResult {
			T::ManagerOrigin::ensure_origin(origin)?;
			Lottery::<T>::try_mutate(|lottery| -> DispatchResult {
				ensure!(lottery.is_none(), Error::<T>::InProgress);
				let index = LotteryIndex::<T>::get();
				let new_index = index.checked_add(1).ok_or(ArithmeticError::Overflow)?;
				let start = frame_system::Pallet::<T>::block_number();
				// Use new_index to more easily track everything with the current state.
				*lottery = Some(LotteryConfig { price, start, length, delay, repeat });
				LotteryIndex::<T>::put(new_index);
				Ok(())
			})?;
			// Make sure pot exists.
			let lottery_account = Self::account_id();
			if T::Currency::total_balance(&lottery_account).is_zero() {
				let _ =
					T::Currency::deposit_creating(&lottery_account, T::Currency::minimum_balance());
			}
			Self::deposit_event(Event::<T>::LotteryStarted);
			Ok(())
		}
```

**File:** substrate/frame/lottery/src/lib.rs (L437-449)
```rust
	/// Logic for buying a ticket.
	fn do_buy_ticket(caller: &T::AccountId, call: &<T as Config>::RuntimeCall) -> DispatchResult {
		// Check the call is valid lottery
		let config = Lottery::<T>::get().ok_or(Error::<T>::NotConfigured)?;
		let block_number = frame_system::Pallet::<T>::block_number();
		ensure!(
			block_number < config.start.saturating_add(config.length),
			Error::<T>::AlreadyEnded
		);
		ensure!(T::ValidateCall::validate_call(call), Error::<T>::InvalidCall);
		let call_index = Self::call_to_index(call)?;
		let ticket_count = TicketsCount::<T>::get();
		let new_ticket_count = ticket_count.checked_add(1).ok_or(ArithmeticError::Overflow)?;
```

**File:** substrate/frame/lottery/src/lib.rs (L484-509)
```rust
	fn choose_account() -> Option<T::AccountId> {
		match Self::choose_ticket(TicketsCount::<T>::get()) {
			None => None,
			Some(ticket) => Tickets::<T>::get(ticket),
		}
	}

	/// Randomly choose a winning ticket from among the total number of tickets.
	/// Returns `None` if there are no tickets.
	fn choose_ticket(total: u32) -> Option<u32> {
		if total == 0 {
			return None;
		}
		let mut random_number = Self::generate_random_number(0);

		// Best effort attempt to remove bias from modulus operator.
		for i in 1..T::MaxGenerateRandom::get() {
			if random_number < u32::MAX - u32::MAX % total {
				break;
			}

			random_number = Self::generate_random_number(i);
		}

		Some(random_number % total)
	}
```

**File:** substrate/frame/lottery/src/lib.rs (L511-522)
```rust
	/// Generate a random number from a given seed.
	/// Note that there is potential bias introduced by using modulus operator.
	/// You should call this function with different seed values until the random
	/// number lies within `u32::MAX - u32::MAX % n`.
	/// TODO: deal with randomness freshness
	/// https://github.com/paritytech/substrate/issues/8311
	fn generate_random_number(seed: u32) -> u32 {
		let (random_seed, _) = T::Randomness::random(&(T::PalletId::get(), seed).encode());
		let random_number = <u32>::decode(&mut random_seed.as_ref())
			.expect("secure hashes should always be bigger than u32; qed");
		random_number
	}
```

**File:** substrate/frame/babe/src/randomness.rs (L56-82)
```rust
/// Randomness usable by on-chain code that **does not depend** upon finality and takes
/// action based upon on-chain commitments made during the previous epoch.
///
/// All randomness is relative to commitments to any other inputs to the computation: If
/// Alice samples randomness near perfectly using radioactive decay, but then afterwards
/// Eve selects an arbitrary value with which to xor Alice's randomness, then Eve always
/// wins whatever game they play.
///
/// All input commitments used with `RandomnessFromOneEpochAgo` should come from at least
/// two epochs ago, although the previous epoch might work in special cases under
/// additional assumption.
///
/// All users learn `RandomnessFromOneEpochAgo` at the end of the previous epoch, although
/// some block producers learn it several block earlier.
///
/// Adversaries with enough block producers could bias this randomness by choosing upon
/// what their block producers build at either the end of the previous epoch or the
/// beginning of the current epoch, or electing to skipping some of their own block
/// production slots towards the end of the previous epoch.
///
/// Adversaries should not possess many block production slots towards the beginning or
/// end of every epoch, but they possess some influence over when they possess more slots.
///
/// As an example usage, we determine parachain auctions ending times in Polkadot using
/// `RandomnessFromOneEpochAgo` because it reduces bias from `ParentBlockRandomness` and
/// does not require the extra finality delay of `RandomnessFromTwoEpochsAgo`.
pub struct RandomnessFromOneEpochAgo<T>(core::marker::PhantomData<T>);
```

**File:** substrate/frame/babe/src/randomness.rs (L143-151)
```rust
impl<T: Config> RandomnessT<T::Hash, BlockNumberFor<T>> for RandomnessFromOneEpochAgo<T> {
	fn random(subject: &[u8]) -> (T::Hash, BlockNumberFor<T>) {
		let mut subject = subject.to_vec();
		subject.reserve(RANDOMNESS_LENGTH);
		subject.extend_from_slice(&NextRandomness::<T>::get()[..]);

		(T::Hashing::hash(&subject[..]), EpochStart::<T>::get().1)
	}
}
```
