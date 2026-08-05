All citations in the claim are verified accurate against the actual code in this repository. The code matches exactly what's described.

Audit Report

## Title
`pallet-lottery` picks the winner using randomness whose freshness is never validated against the ticket-purchase window - ([File: substrate/frame/lottery/src/lib.rs])

## Summary
`Pallet::generate_random_number` calls `T::Randomness::random(...)` and discards the returned `known_since` block number without ever checking that the randomness was determined after the ticket-buying window (`config.start + config.length`) closed. This is confirmed at [1](#0-0) , and the pallet's own TODO acknowledges the gap at [2](#0-1) .

## Finding Description
`on_initialize` triggers payout as soon as `payout_block <= n` with no freshness check, calling `Self::choose_account()` which in turn calls `choose_ticket` → `generate_random_number`: [3](#0-2)  and [4](#0-3) . The `Randomness` trait explicitly documents that the returned block number must be checked against the last commitment time before the seed is trusted: [5](#0-4) . The `LotteryConfig.delay` field is specifically intended to allow fresh randomness to accumulate, per its doc comment: [6](#0-5) . By contrast, `polkadot/runtime/common/src/auctions` solves the identical problem correctly by checking `late_end <= known_since` before trusting the seed, deferring otherwise: [7](#0-6) . `pallet-lottery` has no equivalent guard.

## Impact Explanation
If a runtime configures `Config::Randomness` with a source whose seed can be known or predicted before the ticket-buying window closes (relative to `delay`), an unprivileged participant able to call the public `buy_ticket` extrinsic can bias the outcome of `random_number % total` toward their own ticket index, causing the pot (`lottery_balance`) to be paid to an attacker-controlled account instead of a fairly-selected winner — a duplicate/incorrect settlement to the wrong beneficiary via public, ungated randomness use.

## Likelihood Explanation
Exploitability is contingent on the concrete `Randomness` implementation wired into `Config::Randomness` by the runtime integrator (e.g., short-`delay` configurations paired with low-influence sources). The structural gap — missing `known_since` validation — exists in the pallet code itself regardless of runtime, matching the pattern the pallet's own TODO comment (referencing paritytech/substrate#8311) acknowledges as unresolved.

## Recommendation
Capture `known_since` from `T::Randomness::random` in `generate_random_number`/`choose_ticket` and refuse to finalize the payout in `on_initialize` unless `known_since >= config.start.saturating_add(config.length)`, deferring to a later block otherwise — mirroring `auctions::check_auction_end`'s `late_end <= known_since` deferral pattern.

## Proof of Concept
1. Configure a runtime with `pallet_lottery::Config::Randomness` set to a low-influence/deterministic source (e.g., `RandomnessFromOneEpochAgo`) and a `delay` shorter than the epoch length so the payout-block seed is already fixed before `config.start + config.length`.
2. Call `start_lottery` with a short `length`/`delay`.
3. Before the ticket window closes, read the fixed epoch randomness and compute the expected `generate_random_number(0)` result for the payout block.
4. Time ticket purchases (`buy_ticket`) so `TicketsCount` at window close makes `random_number % total` resolve to the attacker's ticket index.
5. At `payout_block`, `on_initialize` transfers `lottery_balance` to the attacker without any `known_since` freshness check having been performed.

### Citations

**File:** substrate/frame/lottery/src/lib.rs (L89-93)
```rust
	/// Length of the lottery (start + length = end).
	length: BlockNumber,
	/// Delay for choosing the winner of the lottery. (start + length + delay = payout).
	/// Randomness in the "payout" block will be used to determine the winner.
	delay: BlockNumber,
```

**File:** substrate/frame/lottery/src/lib.rs (L243-263)
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

						TicketsCount::<T>::kill();
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

**File:** substrate/frame/lottery/src/lib.rs (L511-516)
```rust
	/// Generate a random number from a given seed.
	/// Note that there is potential bias introduced by using modulus operator.
	/// You should call this function with different seed values until the random
	/// number lies within `u32::MAX - u32::MAX % n`.
	/// TODO: deal with randomness freshness
	/// https://github.com/paritytech/substrate/issues/8311
```

**File:** substrate/frame/lottery/src/lib.rs (L517-522)
```rust
	fn generate_random_number(seed: u32) -> u32 {
		let (random_seed, _) = T::Randomness::random(&(T::PalletId::get(), seed).encode());
		let random_number = <u32>::decode(&mut random_seed.as_ref())
			.expect("secure hashes should always be bigger than u32; qed");
		random_number
	}
```

**File:** substrate/frame/support/src/traits/randomness.rs (L33-37)
```rust
	/// NOTE: The returned seed should only be used to distinguish commitments made before
	/// the returned block number. If the block number is too early (i.e. commitments were
	/// made afterwards), then ensure no further commitments may be made and repeatedly
	/// call this on later blocks until the block number returned is later than the latest
	/// commitment.
```

**File:** polkadot/runtime/common/src/auctions/mod.rs (L539-546)
```rust
				let (raw_offset, known_since) = T::Randomness::random(&b"para_auction"[..]);

				if late_end <= known_since {
					// Our random seed was known only after the auction ended. Good to use.
					let raw_offset_block_number = <BlockNumberFor<T>>::decode(
						&mut raw_offset.as_ref(),
					)
					.expect("secure hashes should always be bigger than the block number; qed");
```
