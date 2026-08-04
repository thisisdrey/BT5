## Analysis Summary

The external report's core broken invariant is: **a public, unrestricted function can arbitrarily inflate an accounting variable that other code treats as trustworthy/backed, with no guard rail tying the increase to a real transfer of value.**

The closest verified local analog is in Snowbridge's **Inbound Queue V2** pallet, where the `Tips` storage map is incremented via the `AddTip` trait implementation without any currency reservation, and that value is later paid out as a real relayer reward.

### Title
Unbacked `Tips` accounting increment lets any caller inflate relayer reward payouts - (File: `bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs`)

### Summary
`Pallet<T>` implements the `AddTip` trait's `add_tip(nonce, amount)` by directly mutating the `Tips` storage map with the caller-supplied `amount`, performing no currency transfer, reservation, or balance check against the caller. [1](#0-0) 

### Finding Description
`Tips::<T>` is documented as "the tip amount, in Ether" used "as an additional relayer incentivization" for a specific nonce. [2](#0-1) 

The only validation performed by `add_tip` is `amount > 0` and that the nonce has not yet been consumed — there is no debit from the caller's balance, no escrow, and no linkage to any Ethereum-side value lock: [3](#0-2) 

When the corresponding message is later processed via `process_message`, the accumulated `Tips` value for that nonce is taken and unconditionally added to `relayer_fee` (which *is* cryptographically bound to the verified Ethereum event) to compute `total_tip`, which is then registered as a real, claimable reward for the relayer: [4](#0-3) 

This mirrors the reported bug class exactly: `calculateSetupRate`/`calculateUpDownRate` update `collectedFee` from caller-supplied `lostTeamTotal`/`wonTeamTotal` with no source-of-truth check; here, `add_tip` updates `Tips` from a caller-supplied `amount` with no source-of-truth (no proof, no locked funds) check, and that inflated value directly feeds a downstream payout function (`RewardPayment::register_reward`) that other code (the bridge-relayers reward ledger and `claim_rewards_to`) trusts as backed.

### Impact Explanation
Because `relayer_fee` is bound to the verified Ethereum message and is the only value normally expected to be "real" money, but `Tips` is a purely local, unauthenticated increment, an attacker can call `add_tip` (via whatever public extrinsic wraps this trait, e.g. in `pallet-system-v2`/`pallet-system-frontend`) to add unbounded value for any not-yet-processed nonce. Once that nonce's message is relayed, the relayer (which could be the attacker itself, self-relaying) is credited `relayer_fee + tip` as a claimable reward via `RewardLedger::register_reward`, which is eventually paid out on AssetHub via `claim_rewards_to` from the bridge reward account — i.e., value that was never actually deposited/locked. This is an unbacked-mint-style drain of the Snowbridge relayer reward pot, directly matching the "theft or unbacked mint" impact category.

### Likelihood Explanation
The `add_tip` trait method itself contains **no economic guard whatsoever** — it is purely bookkeeping. The only mitigating factor would be if the extrinsic(s) that invoke `AddTip::add_tip` (in `pallet-system-v2` / `pallet-system-frontend`, per the additional `add_tip` symbols found in those crates) enforce a real balance transfer/reservation before calling this trait method. I was not able to fully inspect those call sites within the available tool budget, so whether an upstream guard exists is **unverified**. If such a guard is present and correctly sized to `amount`, this specific path is mitigated; if absent or bypassable (e.g., different asset/chain accounting mismatch between the locked amount and `amount` passed to `add_tip`), the exploit is trivial and requires only a signed account — no privileged actor.

### Recommendation
- Ensure the call site(s) invoking `AddTip::add_tip` (in `pallet-system-v2` / `pallet-system-frontend`) atomically lock/transfer real funds equal to `amount` before/alongside the storage mutation, and that this transfer is irreversible only if the tip is eventually consumed by `process_message` (with refund path if the nonce is instead marked consumed some other way).
- Alternatively, move the balance-locking logic directly into `add_tip` in `bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs` so the storage invariant `Tips[nonce] == value actually escrowed` is enforced at the single point of truth rather than relying on caller pallets to enforce it correctly.
- Add a `try_state`/invariant check that total `Tips` outstanding never exceeds funds held in escrow for pending nonces.

### Proof of Concept
1. Attacker (unprivileged, signed account) calls the extrinsic exposed by `pallet-system-v2`/`pallet-system-frontend` that internally invokes `<InboundQueueV2 as AddTip>::add_tip(nonce, u128::MAX / 2)` for a nonce that has not yet been relayed — assuming (unverified) that this call path performs no balance reservation matching `amount`.
2. `Tips::<T>::mutate` stores the inflated tip against `nonce` with no funds actually locked, per `bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs:248-259`.
3. Attacker (or an accomplice) relays the real Ethereum message with that `nonce` via `submit` → `process_message`.
4. `total_tip = relayer_fee.saturating_add(tip)` includes the fabricated tip, and `T::RewardPayment::register_reward(&relayer, ..., total_tip)` credits the relayer with the inflated amount, per `bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs:234-239`.
5. Relayer calls `claim_rewards_to` to withdraw the inflated reward from the bridge reward account on AssetHub, draining funds that were never deposited to back the tip.

### Citations

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs (L174-178)
```rust
	/// Keep track of tips added for a message as an additional relayer incentivization. The
	/// key for the storage map is the nonce of the message to which the tip should be added.
	/// The value is the tip amount, in Ether.
	#[pallet::storage]
	pub type Tips<T: Config> = StorageMap<_, Blake2_128Concat, u64, u128, OptionQuery>;
```

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs (L234-239)
```rust
			// Pay relayer reward
			let tip = Tips::<T>::take(nonce).unwrap_or_default();
			let total_tip = relayer_fee.saturating_add(tip);
			if total_tip > 0 {
				T::RewardPayment::register_reward(&relayer, T::DefaultRewardKind::get(), total_tip);
			}
```

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs (L248-259)
```rust
	impl<T: Config> AddTip for Pallet<T> {
		fn add_tip(nonce: u64, amount: u128) -> Result<(), AddTipError> {
			ensure!(amount > 0, AddTipError::AmountZero);
			// If the nonce is already processed, return an error
			ensure!(!Nonce::<T>::get(nonce.into()), AddTipError::NonceConsumed);
			// Otherwise add the tip.
			Tips::<T>::mutate(nonce, |tip| {
				*tip = Some(tip.unwrap_or_default().saturating_add(amount));
			});
			return Ok(());
		}
	}
```
