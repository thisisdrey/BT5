### Title
Predictable `Randomness` source lets any user pre-compute and rig the `pallet-lottery` winner selection - (File: `substrate/frame/lottery/src/lib.rs`)

### Summary
`pallet-lottery` picks the winner of a pooled prize by hashing a seed obtained from `T::Randomness::random(...)` at the payout block and reducing it modulo the number of sold tickets [1](#0-0) . The exact same broken invariant from the PoolTogether report applies here: the value that determines the winner is knowable before it is used, and a normal user can adjust the state it depends on (how many tickets exist, and which address owns the last ticket index) to steer the outcome in their favor, with no admin, validator, or relayer collusion required.

### Finding Description
The winner is chosen in `on_initialize` by:
```
let winner = Self::choose_account().unwrap_or(lottery_account);
```
which calls `choose_ticket(TicketsCount::<T>::get())`, and `generate_random_number` simply calls `T::Randomness::random(&(PalletId, seed).encode())` [2](#0-1) .

The pallet's own doc comment flags this: *"TODO: deal with randomness freshness https://github.com/paritytech/substrate/issues/8311"* [3](#0-2) . This is not a stale TODO — the `Randomness` sources shipped in this repo are explicitly documented as *predictable ahead of the block that consumes them*:
- `pallet_babe::Randomness` storage carries the warning *"This MUST NOT be used for gambling, as it can be influenced by a malicious validator in the short term"* [4](#0-3) .
- `RandomnessFromOneEpochAgo`/`RandomnessFromTwoEpochsAgo` doc states *"All users learn RandomnessFromOneEpochAgo at the end of the previous epoch, although some block producers learn it several blocks earlier"* [5](#0-4) .
- The benchmarking/weights reference the default example wiring to `RandomnessCollectiveFlip::RandomMaterial` [6](#0-5) , a pallet whose randomness is a rolling hash of the last N *already-finalized, public* block hashes — fully computable by anyone off-chain before the payout block executes.

Because the seed is deterministically derivable from already-committed on-chain data (not a secret an admin reveals, but a public value any observer can compute ahead of the deadline), an unprivileged user can:
1. Compute `random_number % total_tickets_expected` for the known `payout_block` in advance.
2. Before that block, use the public `buy_ticket` extrinsic [7](#0-6)  to adjust `TicketsCount` (buying just enough tickets, or timing the last ticket) so that `Tickets::<T>::get(winning_index)` maps to their own account, exactly mirroring the PoolTogether report's manipulation of "distribution of committed draws" via transfers/burns/withdrawals before the reveal — here the manipulation vector is the number/ownership of tickets before the randomness is consumed.

No admin action, validator equivocation, or transaction front-running of a specific victim tx is required: the corrupted value is the "unbiased-looking" `random_number` derived from `T::Randomness`, which is public and predictable strictly before use, breaking the intended invariant that the pot recipient is unpredictable and fair.

### Impact Explanation
This lets an ordinary user divert the entire lottery pot — funds contributed by other participants — to themselves by gaming a public, precomputable RNG rather than a true source of unpredictability, i.e. theft/diversion of pooled user funds to an unintended beneficiary. This directly matches the "theft ... duplicate settlement or payout ... wrong beneficiary or amount" impact category for staking/pools/treasury-style payout logic.

### Likelihood Explanation
Likelihood depends entirely on which `Config::Randomness` implementation a runtime wires into `pallet-lottery`. If wired to `RandomnessCollectiveFlip` (an "insecure" pallet by name, but present and usable) or to `pallet_babe::Randomness`/`RandomnessFromOneEpochAgo`, the seed is knowable ahead of the payout block by any observer, making exploitation straightforward and requiring only ordinary signed extrinsics (`buy_ticket`). The pallet's own acknowledged TODO about "randomness freshness" confirms this is a known, unresolved gap rather than a theoretical one.

### Recommendation
Do not let the pot outcome be determined solely by a seed that is public/predictable strictly before the block that consumes it. Freeze ticket purchases/participation (`TicketsCount`, `Tickets`, `Participants`) for a configurable window before the payout block is reachable, and/or require a randomness source with a finality/reveal delay that post-dates the freeze (e.g. `RandomnessFromTwoEpochsAgo` combined with a hard stop on `buy_ticket` well before the epoch boundary that supplies the randomness), so no ticket-count manipulation can occur after the deciding value becomes computable.

### Proof of Concept
1. Deploy a runtime with `pallet-lottery` configured with `type Randomness = pallet_babe::RandomnessFromOneEpochAgo<Runtime>` (or `RandomnessCollectiveFlip`).
2. `start_lottery` sets `payout_block = start + length + delay` (public, in storage) [8](#0-7) .
3. Off-chain, once the relevant epoch's/collective-flip randomness becomes fixed (which happens before `payout_block`, per the documented "learned at the end of the previous epoch" property), compute `generate_random_number` for every seed in `0..MaxGenerateRandom` exactly as `choose_ticket` does [9](#0-8) , deriving the exact winning ticket index for the currently known `TicketsCount`.
4. Submit/withhold `buy_ticket` calls so that the attacker's account ends up owning `Tickets::<T>::get(winning_index)` before `payout_block`.
5. At `on_initialize(payout_block)`, `choose_account()` resolves to the attacker's address, transferring the whole pot balance to them [10](#0-9) .

### Citations

**File:** substrate/frame/lottery/src/lib.rs (L243-248)
```rust
		fn on_initialize(n: BlockNumberFor<T>) -> Weight {
			Lottery::<T>::mutate(|mut lottery| -> Weight {
				if let Some(config) = &mut lottery {
					let payout_block =
						config.start.saturating_add(config.length).saturating_add(config.delay);
					if payout_block <= n {
```

**File:** substrate/frame/lottery/src/lib.rs (L249-261)
```rust
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

**File:** substrate/frame/lottery/src/lib.rs (L491-522)
```rust
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

**File:** substrate/frame/babe/src/lib.rs (L209-218)
```rust
	/// The epoch randomness for the *current* epoch.
	///
	/// # Security
	///
	/// This MUST NOT be used for gambling, as it can be influenced by a
	/// malicious validator in the short term. It MAY be used in many
	/// cryptographic protocols, however, so long as one remembers that this
	/// (like everything else on-chain) it is public. For example, it can be
	/// used where a number is needed that cannot have been chosen by an
	/// adversary, for purposes such as public-coin zero-knowledge proofs.
```

**File:** substrate/frame/babe/src/randomness.rs (L64-82)
```rust
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

**File:** substrate/frame/lottery/src/weights.rs (L262-271)
```rust
	/// Storage: `RandomnessCollectiveFlip::RandomMaterial` (r:1 w:0)
	/// Proof: `RandomnessCollectiveFlip::RandomMaterial` (`max_values`: Some(1), `max_size`: Some(2594), added: 3089, mode: `MaxEncodedLen`)
	/// Storage: `Lottery::Lottery` (r:1 w:1)
	/// Proof: `Lottery::Lottery` (`max_values`: Some(1), `max_size`: Some(29), added: 524, mode: `MaxEncodedLen`)
	/// Storage: `System::Account` (r:2 w:2)
	/// Proof: `System::Account` (`max_values`: None, `max_size`: Some(128), added: 2603, mode: `MaxEncodedLen`)
	/// Storage: `Lottery::TicketsCount` (r:1 w:1)
	/// Proof: `Lottery::TicketsCount` (`max_values`: Some(1), `max_size`: Some(4), added: 499, mode: `MaxEncodedLen`)
	/// Storage: `Lottery::Tickets` (r:1 w:0)
	/// Proof: `Lottery::Tickets` (`max_values`: None, `max_size`: Some(44), added: 2519, mode: `MaxEncodedLen`)
```
