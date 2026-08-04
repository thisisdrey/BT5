### Title
Insecure/Stale Randomness in `pallet-lottery` Winner Selection Allows Ticket-Purchase Timing Manipulation to Bias Fund Payout - (File: `substrate/frame/lottery/src/lib.rs`)

### Summary
`pallet_lottery` selects the winner of a pot of real funds by calling `T::Randomness::random(...)` and using the modulus of the returned hash against the ticket count [1](#0-0) . The pallet discards the "known since" block number that the `Randomness` trait explicitly requires callers to check before trusting the value for decisions tied to prior commitments [2](#0-1) , and the default runtime wires this pallet to `pallet_insecure_randomness_collective_flip`, whose own documentation states the produced values "do not fulfill the cryptographic requirements for random numbers" and warns that "all bits of the resulting value are entirely manipulatable by the author of the parent block" [3](#0-2) [4](#0-3) . This mirrors the external report's core defect class: fund-determination logic relying on predictable/weakly-random values instead of a properly committed, verifiable randomness source.

### Finding Description
`choose_ticket`/`generate_random_number` compute the winning ticket as `random_number % total` where `random_number` comes straight from `T::Randomness::random(&(PalletId, seed).encode())`, ignoring the returned freshness/`BlockNumber` component entirely [5](#0-4) . The pallet author's own comment acknowledges this gap directly in the source: `// TODO: deal with randomness freshness https://github.com/paritytech/substrate/issues/8311` [6](#0-5) .

The `Randomness` trait contract is explicit that the caller (not the randomness provider) is responsible for ensuring "no further commitments may be made" until a randomness value's `known_since` block is later than the last commitment (i.e., the last ticket purchase) [7](#0-6) . `pallet_lottery` never performs this check. The `delay` parameter of `start_lottery` is manager-configurable to any value, including `0` [8](#0-7) , so the draw (executed in `on_initialize`, which runs before extrinsics of that block) can occur immediately after the ticket-buying window closes (`block_number < config.start + config.length` gate in `do_buy_ticket`) [9](#0-8) .

With the default `RandomnessCollectiveFlip` source, the seed is derived from a 81-block ring buffer of parent hashes that is mixed at every block [10](#0-9) ; the overwhelming majority of these 81 inputs are already public/finalized well before the ticket-sale deadline, with only the last one or two hashes still unknown at the moment the sale closes. Because `pallet_lottery` never enforces that the randomness `known_since` postdates the ticket-buying deadline, an unprivileged participant who is also a late/large ticket buyer can observe the already-fixed majority of the mix, and by choosing exactly how many additional tickets to purchase in the final eligible blocks (thereby controlling `total`), bias which modulus outcome (`random_number % total`) lands on their own held ticket index — all without needing to be a block author, validator, or collator.

### Impact Explanation
The lottery pot holds real, user-contributed `Currency` funds that are transferred in full to the computed "winner" via `T::Currency::transfer` [11](#0-10) . Biasing the winner-selection modulus lets an ordinary, unprivileged user redirect the entire pot to themselves instead of to the honestly "rightful" random beneficiary, i.e., a fund-diversion/theft outcome from a public-entrypoint pallet — matching the "theft… or wrong beneficiary/amount" impact category for balances/pot payouts.

### Likelihood Explanation
Exploitation requires no privileged role, key compromise, or malicious validator/collator — only a normal signed account able to call the already-public `buy_ticket` extrinsic multiple times near the end of the sale window and observe already-finalized chain state, which is exactly the attacker capability the underlying `Randomness` trait doc warns callers to guard against by checking `known_since`. The lottery pallet's own source comment flags this exact freshness gap as an acknowledged unresolved TODO, indicating the weakness is real and has persisted in the codebase rather than being purely theoretical.

### Recommendation
In `generate_random_number`/`choose_ticket`, capture and enforce the `known_since` block number returned by `T::Randomness::random`, rejecting/deferring the draw unless `known_since` is strictly later than the last block in which a ticket could be purchased (`config.start + config.length`). Alternatively, require `delay` to be large enough (parameterized against the configured `Randomness` implementation's freshness window, e.g. `RANDOM_MATERIAL_LEN` for the collective-flip pallet) to guarantee no ticket-sale-window data can leak into the seed, or require use of a VRF-based `Randomness` source (e.g., BABE's `ParentBlockRandomness`/epoch randomness) for any runtime that assigns real value to `pallet_lottery`.

### Proof of Concept
1. Deploy a runtime with `pallet_lottery` configured with `type Randomness = RandomnessCollectiveFlip` (as in the reference node runtime) [12](#0-11) .
2. `ManagerOrigin` starts a lottery with `delay = 0` (or any small value) via `start_lottery`.
3. Multiple honest participants buy tickets throughout the sale window; RandomMaterial accumulates 80 of the 81 relevant parent hashes publicly as blocks are finalized.
4. In the final eligible block(s) before `start + length`, the attacker computes, for several candidate values of `k` additional self-purchased tickets, what `total` and thus what `random_number % total` would resolve to, using the already-known 80/81 mixed inputs.
5. The attacker submits exactly the `k` ticket purchases (within `MaxCalls`/balance limits) that make their own already-held ticket index equal to the predicted winning index.
6. At `on_initialize` of the payout block, `choose_account`/`choose_ticket` [13](#0-12)  resolves to the attacker's ticket, and the full pot is transferred to the attacker instead of being a fair random draw.

### Citations

**File:** substrate/frame/lottery/src/lib.rs (L243-261)
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

**File:** substrate/frame/lottery/src/lib.rs (L349-368)
```rust
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
```

**File:** substrate/frame/lottery/src/lib.rs (L438-445)
```rust
	fn do_buy_ticket(caller: &T::AccountId, call: &<T as Config>::RuntimeCall) -> DispatchResult {
		// Check the call is valid lottery
		let config = Lottery::<T>::get().ok_or(Error::<T>::NotConfigured)?;
		let block_number = frame_system::Pallet::<T>::block_number();
		ensure!(
			block_number < config.start.saturating_add(config.length),
			Error::<T>::AlreadyEnded
		);
```

**File:** substrate/frame/lottery/src/lib.rs (L481-522)
```rust
	/// Randomly choose a winning ticket and return the account that purchased it.
	/// The more tickets an account bought, the higher are its chances of winning.
	/// Returns `None` if there is no winner.
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

**File:** substrate/frame/support/src/traits/randomness.rs (L26-53)
```rust
pub trait Randomness<Output, BlockNumber> {
	/// Get the most recently determined random seed, along with the time in the past
	/// since when it was determinable by chain observers.
	///
	/// `subject` is a context identifier and allows you to get a different result to
	/// other callers of this function; use it like `random(&b"my context"[..])`.
	///
	/// NOTE: The returned seed should only be used to distinguish commitments made before
	/// the returned block number. If the block number is too early (i.e. commitments were
	/// made afterwards), then ensure no further commitments may be made and repeatedly
	/// call this on later blocks until the block number returned is later than the latest
	/// commitment.
	fn random(subject: &[u8]) -> (Output, BlockNumber);

	/// Get the basic random seed.
	///
	/// In general you won't want to use this, but rather `Self::random` which allows
	/// you to give a subject for the random result and whose value will be
	/// independently low-influence random from any other such seeds.
	///
	/// NOTE: The returned seed should only be used to distinguish commitments made before
	/// the returned block number. If the block number is too early (i.e. commitments were
	/// made afterwards), then ensure no further commitments may be made and repeatedly
	/// call this on later blocks until the block number returned is later than the latest
	/// commitment.
	fn random_seed() -> (Output, BlockNumber) {
		Self::random(&[][..])
	}
```

**File:** substrate/frame/insecure-randomness-collective-flip/src/lib.rs (L127-158)
```rust
impl<T: Config> Randomness<T::Hash, BlockNumberFor<T>> for Pallet<T> {
	/// This randomness uses a low-influence function, drawing upon the block hashes from the
	/// previous 81 blocks. Its result for any given subject will be known far in advance by anyone
	/// observing the chain. Any block producer has significant influence over their block hashes
	/// bounded only by their computational resources. Our low-influence function reduces the actual
	/// block producer's influence over the randomness, but increases the influence of small
	/// colluding groups of recent block producers.
	///
	/// WARNING: Hashing the result of this function will remove any low-influence properties it has
	/// and mean that all bits of the resulting value are entirely manipulatable by the author of
	/// the parent block, who can determine the value of `parent_hash`.
	fn random(subject: &[u8]) -> (T::Hash, BlockNumberFor<T>) {
		let block_number = frame_system::Pallet::<T>::block_number();
		let index = block_number_to_index::<T>(block_number);

		let hash_series = RandomMaterial::<T>::get();
		let seed = if !hash_series.is_empty() {
			// Always the case after block 1 is initialized.
			hash_series
				.iter()
				.cycle()
				.skip(index)
				.take(RANDOM_MATERIAL_LEN as usize)
				.enumerate()
				.map(|(i, h)| (i as i8, subject, h).using_encoded(T::Hashing::hash))
				.triplet_mix()
		} else {
			T::Hash::default()
		};

		(seed, block_number.saturating_sub(RANDOM_MATERIAL_LEN.into()))
	}
```

**File:** substrate/bin/node/runtime/src/lib.rs (L1936-1953)
```rust
parameter_types! {
	pub const LotteryPalletId: PalletId = PalletId(*b"py/lotto");
	pub const MaxCalls: u32 = 10;
	pub const MaxGenerateRandom: u32 = 10;
}

impl pallet_lottery::Config for Runtime {
	type PalletId = LotteryPalletId;
	type RuntimeCall = RuntimeCall;
	type Currency = Balances;
	type Randomness = RandomnessCollectiveFlip;
	type RuntimeEvent = RuntimeEvent;
	type ManagerOrigin = EnsureRoot<AccountId>;
	type MaxCalls = MaxCalls;
	type ValidateCall = Lottery;
	type MaxGenerateRandom = MaxGenerateRandom;
	type WeightInfo = pallet_lottery::weights::SubstrateWeight<Runtime>;
}
```
