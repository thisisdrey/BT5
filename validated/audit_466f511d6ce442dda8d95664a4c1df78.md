### Title
`add_tip` in Snowbridge `system-v2` pallet trusts caller-supplied `amount` without any on-chain value backing, inflating relayer reward accounting - ([File: bridges/snowbridge/pallets/system-v2/src/lib.rs])

### Summary
The Snowbridge V2 flow pays relayers in Ether-denominated rewards computed from `relayer_fee` (from the verified Ethereum message) plus a `tip` value stored per-nonce. The `tip` is populated exclusively through `Pallet::add_tip` in `bridges/snowbridge/pallets/system-v2/src/lib.rs`, which accepts a bare `u128 amount` parameter and immediately forwards it to `InboundQueue::add_tip`/`OutboundQueue::add_tip`, incrementing the `Tips` storage that is later added straight into the relayer's payable reward. Nothing in this pallet locks, withdraws, or otherwise verifies that `amount` corresponds to any value actually escrowed on-chain — exactly the same class of bug as the external report, where a value from one accounting bucket (attacker/caller-supplied numeric input) is blindly folded into a payout total (`ethAmount`) that is assumed, but never verified, to be backed by a real corresponding asset movement.

### Finding Description
`add_tip` is defined as: [1](#0-0) 

The only gate is `T::FrontendOrigin::ensure_origin(origin)`, which converts an XCM origin to a `Location` — it does not perform any balance check, reserve, or transfer of `amount`. The function then does: [2](#0-1) 

which calls into the inbound queue's `AddTip` implementation: [3](#0-2) 

This directly mutates the `Tips` storage map by the full requested `amount`, with only a zero-check and a "nonce not yet consumed" check — no verification that `amount` was ever escrowed, transferred, or burned anywhere in this call path.

Later, when the corresponding message is processed, the stored tip is unconditionally combined with the relayer_fee and registered as a payable reward: [4](#0-3) 

This mirrors the reported bug precisely: `swap()` trusted `order.isETHSell` to gate whether ERC20 proceeds should be folded into the ETH-denominated `ethAmount` returned to the caller, without validating that the flag matched reality. Here, `add_tip`/`process_message` trust the numeric `amount` argument to represent Ether-equivalent value that was actually paid, without validating that any matching asset transfer occurred within this pallet's own execution — the entire backing assumption is pushed onto a separate call site (the AssetHub-side "system-frontend" pallet is expected to withdraw funds before dispatching this call via XCM `Transact`), but `system-v2::add_tip` itself enforces nothing.

### Impact Explanation
If the cross-chain trust boundary between the AssetHub "system-frontend" pallet (source of the `Transact` origin) and this BridgeHub pallet is ever exercised with a mismatched or forged `amount` — whether via a bug in the frontend's own withdrawal logic, a version/encoding mismatch in the XCM-dispatched call, or reuse of a `FrontendOrigin`-satisfying location without a corresponding debit — a relayer can be credited Ether rewards that were never actually deposited or locked, i.e., an unbacked mint of bridge reward value paid out of the Snowbridge Ether pot. This is a direct match to the "theft or unbacked mint" and "duplicate settlement or payout" impact categories.

### Likelihood Explanation
`add_tip` on BridgeHub performs zero independent economic validation; it is entirely dependent on the calling side (the AssetHub `system-frontend` pallet and the XCM `Transact` dispatch path) to correctly withdraw the stated `amount` before invoking this extrinsic. Because the value-conservation guarantee lives entirely outside this function and is not re-checked here, any weakness in that upstream accounting (a code path not covered by this scan) directly and silently inflates payable rewards with no additional guard in this pallet. The likelihood is moderate: it requires a defect or omission in the paired frontend-side charge logic, which was not verifiable within the scope of the files inspected here.

### Recommendation
Have `system-v2::add_tip` (or the inbound/outbound `AddTip` implementations) require and check proof of an actual, atomic asset transfer/lock for `amount` within the same call, rather than trusting a bare numeric parameter forwarded across a pallet/XCM boundary. At minimum, add invariant tests/assertions that the total value registered via `RewardPayment::register_reward` never exceeds the sum of relayer_fee actually present in verified Ethereum messages plus tips that were provably escrowed on this chain.

### Proof of Concept
Cannot be fully constructed from the inspected code alone: exploitation requires demonstrating that some caller can satisfy `T::FrontendOrigin` for `system-v2::add_tip` without the AssetHub-side `system-frontend` pallet having first withdrawn the equivalent `amount` from the sender. I was not able to inspect the `system-frontend` pallet's withdrawal/debit logic within the available tool budget to confirm whether such a mismatch is currently reachable; this is the main open item that would need verification (e.g., in a Devin session with full repo access) before treating this as a confirmed, exploitable unbacked-mint path rather than a design-level trust gap.

### Citations

**File:** bridges/snowbridge/pallets/system-v2/src/lib.rs (L251-281)
```rust
		#[pallet::call_index(3)]
		#[pallet::weight(<T as pallet::Config>::WeightInfo::add_tip())]
		pub fn add_tip(
			origin: OriginFor<T>,
			sender: AccountIdOf<T>,
			message_id: MessageId,
			amount: u128,
		) -> DispatchResult {
			T::FrontendOrigin::ensure_origin(origin)?;

			let result = match message_id {
				Inbound(nonce) => <T as pallet::Config>::InboundQueue::add_tip(nonce, amount),
				Outbound(nonce) => <T as pallet::Config>::OutboundQueue::add_tip(nonce, amount),
			};

			if let Err(ref e) = result {
				tracing::debug!(target: LOG_TARGET, ?e, ?message_id, ?amount, "error adding tip");
				LostTips::<T>::mutate(&sender, |lost_tip| {
					*lost_tip = lost_tip.saturating_add(amount);
				});
			}

			Self::deposit_event(Event::<T>::TipProcessed {
				sender,
				message_id,
				amount,
				success: result.is_ok(),
			});

			Ok(())
		}
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

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs (L248-258)
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
```
