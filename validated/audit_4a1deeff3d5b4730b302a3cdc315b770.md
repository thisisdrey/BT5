### Title
`pallet-lottery` picks the winner using randomness whose freshness is never validated against the ticket-purchase window - ([File: substrate/frame/lottery/src/lib.rs])

### Summary
The Entropy.sol report's core broken invariant is: a value meant to bind the result to a *specific, already-closed* commitment window (the block hash) is consumed without checking that it was actually determined *after* that window closed, letting the caller pick between multiple usable outcomes. `pallet-lottery`'s `generate_random_number` reproduces this exact broken invariant: it calls `T::Randomness::random(...)` and discards the returned `known_since` block number, so the pallet never verifies that the randomness used to pick the winner was determined after the lottery's ticket-buying window ended.

### Finding Description
`Pallet::choose_ticket` / `Pallet::generate_random_number` compute the winning ticket like this: [1](#0-0) 

```rust
fn generate_random_number(seed: u32) -> u32 {
    let (random_seed, _) = T::Randomness::random(&(T::PalletId::get(), seed).encode());
    ...
}
```

The `Randomness` trait's own documentation explicitly warns callers that the returned block number ("known since") must be checked against the time of the last commitment before the seed is trusted: [2](#0-1) 

The lottery config even models a `delay` field specifically intended to let sufficiently-fresh randomness accumulate before the winner is drawn ("Randomness in the 'payout' block will be used to determine the winner"): [3](#0-2) 

However, `on_initialize` simply calls `choose_account()` as soon as `payout_block <= n`, with no check that the randomness source's `known_since` value is later than the ticket-buying deadline (`config.start + config.length`): [4](#0-3) 

Compare this to `polkadot/runtime/common/src/auctions`, which faces the identical problem (picking a random ending offset after a bidding period) and explicitly guards against it by checking `late_end <= known_since` before using the seed, and otherwise defers using it: [5](#0-4) 

`pallet-lottery` has no equivalent check — the exact freshness-validation guard that `Entropy.sol` was missing (and which `auctions.rs` correctly implements) is absent here. The pallet's own source code even carries an open TODO acknowledging this: [6](#0-5) 

### Impact Explanation
If a runtime wires `pallet_lottery::Config::Randomness` to a source whose seed can be predictable or known before the ticket-buying window ends relative to `delay` (e.g. a short `delay`, or a `Randomness` implementation such as `RandomnessFromOneEpochAgo`/`ParentBlockRandomness`/the insecure collective-flip pallet, none of which guarantee the seed postdates the last ticket purchase unless the caller checks `known_since`), an unprivileged participant can:
1. Predict (or, in the case of low-influence sources, directly observe) the seed that will be used at the payout block before the ticket window closes.
2. Choose whether/when/how many tickets to buy (and thus influence `TicketsCount`/ticket indices) so that the resulting `random_number % total` maps to their own ticket.

This lets an unprivileged user bias which account wins the pot — a direct "wrong beneficiary" outcome for value (`lottery_balance`) held in the pallet's pot account, achieved purely through public entry points (`buy_ticket`) without needing a malicious validator/collator, matching the required impact class (unbacked/duplicate settlement to the wrong beneficiary via public underpriced/ungated randomness use).

### Likelihood Explanation
The likelihood is dependent on runtime configuration of `Config::Randomness`. Wherever pallet-lottery is deployed with a `Randomness` provider that does not guarantee freshness relative to the buy-window end (the pallet does not itself enforce or document this requirement, unlike `auctions.rs` which does the check inline), the missing freshness check is deterministically exploitable by any signed account able to call `buy_ticket`. The bug is a structural gap in the pallet logic itself (present regardless of runtime), so it is a real code-level defect even though full exploitability depends on the concrete `Randomness` type chosen by a given runtime integrator.

### Recommendation
- Short term: In `choose_ticket`/`generate_random_number`, capture the `known_since` block number returned by `T::Randomness::random` and refuse to finalize the payout (retry on a later block, mirroring `auctions::check_auction_end`'s deferral pattern) unless `known_since >= config.start.saturating_add(config.length)`.
- Long term: Update the `Randomness` trait documentation/usages across the codebase (lottery, contracts `seal_random`, etc.) to enforce, at the type level or via a helper, that consumers must validate freshness before trusting a random value for any commit/reveal-style selection, consistent with the pattern already used in `auctions.rs`.

### Proof of Concept
1. Configure a runtime with `pallet_lottery::Config::Randomness = pallet_babe::RandomnessFromOneEpochAgo<Runtime>` (or a similarly deterministic/low-influence source) and a `delay` shorter than the epoch length such that the seed for the payout block is already committed on-chain before `config.start + config.length` is reached.
2. Call `start_lottery` with a short `length` and `delay`.
3. Before the ticket-buying window closes, read the current epoch's `Randomness` value (public storage) and compute what `generate_random_number(0)` will evaluate to at the payout block (deterministic given `PalletId` and seed 0, since the epoch randomness is already fixed).
4. Choose to buy (or skip buying) a ticket, and control `TicketsCount` at the moment of your last purchase, such that `random_number % total` resolves to your own ticket index.
5. At `payout_block`, `on_initialize` calls `choose_account` and transfers `lottery_balance` to the attacker's account, without ever having checked that the randomness was determined after ticket purchases closed.

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
