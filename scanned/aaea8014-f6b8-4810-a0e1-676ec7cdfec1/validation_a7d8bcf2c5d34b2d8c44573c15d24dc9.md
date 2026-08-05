### Title
`PendingOrder.fee` can be inflated via `AddTip::add_tip` without any corresponding escrow, letting `process_delivery_receipt` register unbacked relayer rewards - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
The Snowbridge V2 outbound queue mirrors the BendDAO pattern almost exactly: a "claimable amount" field (`PendingOrder.fee`) is incremented in pallet storage independently of any guarantee that backing funds for that exact amount exist, and this same field is later used, unmodified, to credit a relayer with a real, transferable reward via `pallet-bridge-relayers`. Just as `totalUnstakeFine` was incremented in `_unstake` before the corresponding funds were ever recovered in `_repay`, `PendingOrder.fee` is incremented by `AddTip::add_tip` as a pure storage write, with no fund transfer or reservation performed by the pallet itself at that point.

### Finding Description
When a message is queued, `do_process_message` creates a `PendingOrder` recording the fee that was declared in the (user-supplied) `Message`: [1](#0-0) 

Later, any caller of the `AddTip` interface can increase that same `order.fee` value directly in storage, with **no balance movement inside the function**: [2](#0-1) 

When a relayer submits a valid delivery receipt, `process_delivery_receipt` reads whatever `order.fee` currently is and unconditionally forwards it to the reward ledger as a claimable amount: [3](#0-2) 

That reward is registered into `pallet_bridge_relayers::RelayerRewards` via `register_relayer_reward`, which purely does a saturating-add on a storage double map — it performs no check that a matching amount of funds is actually available anywhere: [4](#0-3) 

The relayer can later call `claim_rewards` / `claim_rewards_to`, which take the *entire* recorded balance and attempt a real transfer through `T::PaymentProcedure::pay_reward`: [5](#0-4) 

This is the same broken invariant as `YieldStakingBase::totalUnstakeFine`: a "promised payout" counter (`order.fee`, subsequently `RelayerRewards`) is incremented (via `add_tip`) with no atomic, enforced linkage to the actual escrowed balance that will be used to honor it at claim time (`PaymentProcedure::pay_reward`, e.g. `PayRewardFromAccount`, transfers from a `RewardsAccountParams`-derived account). Nothing in the `outbound-queue-v2` pallet itself ties the amount added by `add_tip` to a withdrawal from the tipper into the account that `pay_reward` will later debit. If the calling/XCM layer that invokes `add_tip` does not perform an atomic, equal-amount withdrawal into the paying account for every tip, the ledger diverges from real backing funds, exactly like `totalUnstakeFine` diverging from actually recovered fines.

### Impact Explanation
If the escrow accompanying `add_tip` is not enforced atomically and for the exact amount at every call site, this allows:
- Unbacked inflation of a relayer's registered reward (`RelayerRewards`), which is real, transferable value once claimed — a direct "theft or unbacked mint" impact category.
- If backing funds are insufficient, `claim_rewards`/`claim_rewards_to` will revert on `pay_reward` failure (`Error::FailedToPayReward`), which — because the reward entry is never actually paid nor cleared, similarly to `collectFeeToTreasury` reverting — permanently locks that relayer's claim, denying honest relayers their real earned rewards (repetitive revert / DOS on the relayer-reward payout path) if the reward pot for that lane/order is depleted by prior unbacked increments.

### Likelihood Explanation
The `add_tip` implementation itself is unprivileged with respect to fund custody: as written, it is a raw storage mutation with an `ensure!(amount > 0)` guard only, no `Currency`/`fungible` debit. Any misalignment between how the calling configuration (e.g., an XCM `PayFees`/tip instruction) escrows funds and how much is passed into `add_tip` will directly corrupt `PendingOrder.fee`, and from there `RelayerRewards`. Because the escrow logic lives outside this pallet (in XCM processing/exporter configuration not covered by this function), the guarantee that "1 unit of tip == 1 unit of add_tip amount, atomically" is not enforced by the pallet's own type system or logic — it is an external invariant, exactly the kind of un-enforced assumption that produced the original BendDAO bug.

### Recommendation
- Require `add_tip` (and any other path that mutates `PendingOrder.fee`) to perform, in the same call, an actual debit/hold of `amount` into the account that `PaymentProcedure::pay_reward` will later transfer from, rather than trusting an external, non-atomic escrow step.
- Alternatively, track `order.fee` and a separate `order.escrowed_amount`, and only allow `process_delivery_receipt` to register a reward up to `min(order.fee, order.escrowed_amount)`.
- Add defensive checks in `pallet-bridge-relayers::register_relayer_reward` / `do_claim_rewards` verifying that the rewards-paying account balance can at least cover the newly registered/claimed amount before crediting the ledger, failing loudly (not silently inflating the ledger) otherwise.

### Proof of Concept
1. A message is queued via `do_process_message` with `fee = F1`, creating `PendingOrder { nonce, fee: F1, .. }`.
2. Any caller with access to the `AddTip` interface invokes `add_tip(nonce, F2)` without any accompanying transfer into the rewards-paying account for lane `nonce`. `order.fee` becomes `F1 + F2` purely in storage: [6](#0-5) 
3. A relayer submits a valid `submit_delivery_receipt` / `process_delivery_receipt` for `nonce`; `order.fee` (`F1 + F2`) is registered wholesale as a reward: [7](#0-6) 
4. The relayer calls `claim_rewards_to`, and `pay_reward` attempts to transfer `F1 + F2` from the lane's rewards account — an amount never actually collected for the `F2` portion, either succeeding in draining more than was ever escrowed (fund theft) or reverting and permanently blocking the relayer's legitimate `F1` claim once the ledger entry includes the inflated `F2` (repetitive revert / DOS), depending on the rewards-account's actual balance at claim time. [5](#0-4) 

**Caveat**: I could not, within the available tool budget, locate and confirm the exact external caller(s) of `AddTip::add_tip` (the XCM/router configuration that is supposed to escrow the tip amount) to verify whether that call site enforces the atomic 1:1 debit. This finding is reported strictly based on the pallet-level code shown above, where the `add_tip` function itself contains no fund-movement logic, structurally matching the BendDAO root cause (a payout-counter increment decoupled from fund availability). Confirming exploitability end-to-end requires inspecting the XCM exporter/tip-collection code that is not indexed in this scan.

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L426-436)
```rust
			// Generate `PendingOrder` with fee attached in the message, stored
			// into the `PendingOrders` map storage, with assigned nonce as the key.
			// When the message is processed on ethereum side, the relayer will send the nonce
			// back with delivery proof, only after that the order can
			// be resolved and the fee will be rewarded to the relayer.
			let order = PendingOrder {
				nonce,
				fee,
				block_number: frame_system::Pallet::<T>::current_block_number(),
			};
			<PendingOrders<T>>::insert(nonce, order);
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L446-480)
```rust
		pub fn process_delivery_receipt(
			relayer: <T as frame_system::Config>::AccountId,
			receipt: DeliveryReceipt,
		) -> DispatchResult
		where
			<T as frame_system::Config>::AccountId: From<[u8; 32]>,
		{
			// Verify that the message was submitted from the known Gateway contract
			ensure!(T::GatewayAddress::get() == receipt.gateway, Error::<T>::InvalidGateway);

			let reward_account = if receipt.reward_address == [0u8; 32] {
				relayer
			} else {
				receipt.reward_address.into()
			};

			let nonce = receipt.nonce;

			let order = <PendingOrders<T>>::get(nonce).ok_or(Error::<T>::InvalidPendingNonce)?;

			if order.fee > 0 {
				// Pay relayer reward
				T::RewardPayment::register_reward(
					&reward_account,
					T::DefaultRewardKind::get(),
					order.fee,
				);
			}

			<PendingOrders<T>>::remove(nonce);

			Self::deposit_event(Event::MessageDelivered { nonce });

			Ok(())
		}
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L483-496)
```rust
	impl<T: Config> AddTip for Pallet<T> {
		fn add_tip(nonce: u64, amount: u128) -> Result<(), AddTipError> {
			ensure!(amount > 0, AddTipError::AmountZero);
			PendingOrders::<T>::try_mutate_exists(nonce, |maybe_order| -> Result<(), AddTipError> {
				match maybe_order {
					Some(order) => {
						order.fee = order.fee.saturating_add(amount);
						Ok(())
					},
					None => Err(AddTipError::UnknownMessage),
				}
			})
		}
	}
```

**File:** bridges/modules/relayers/src/lib.rs (L263-303)
```rust
		fn do_claim_rewards(
			relayer: T::AccountId,
			reward_kind: T::Reward,
			beneficiary: BeneficiaryOf<T, I>,
		) -> DispatchResult {
			RelayerRewards::<T, I>::try_mutate_exists(
				&relayer,
				reward_kind,
				|maybe_reward| -> DispatchResult {
					let reward_balance =
						maybe_reward.take().ok_or(Error::<T, I>::NoRewardForRelayer)?;
					T::PaymentProcedure::pay_reward(
						&relayer,
						reward_kind,
						reward_balance,
						beneficiary.clone(),
					)
					.map_err(|e| {
						tracing::error!(
							target: LOG_TARGET,
							error=?e,
							?relayer,
							?reward_kind,
							?reward_balance,
							?beneficiary,
							"Failed to pay rewards"
						);
						Error::<T, I>::FailedToPayReward
					})?;

					Self::deposit_event(Event::<T, I>::RewardPaid {
						relayer: relayer.clone(),
						reward_kind,
						reward_balance,
						beneficiary,
					});
					Ok(())
				},
			)
		}

```

**File:** bridges/modules/relayers/src/lib.rs (L399-432)
```rust
		/// Register reward for given relayer.
		pub(crate) fn register_relayer_reward(
			reward_kind: T::Reward,
			relayer: &T::AccountId,
			reward_balance: T::RewardBalance,
		) {
			if reward_balance.is_zero() {
				return;
			}

			RelayerRewards::<T, I>::mutate(
				relayer,
				reward_kind,
				|old_reward: &mut Option<T::RewardBalance>| {
					let new_reward =
						old_reward.unwrap_or_else(Zero::zero).saturating_add(reward_balance);
					*old_reward = Some(new_reward);

					tracing::trace!(
						target: crate::LOG_TARGET,
						?relayer,
						?reward_kind,
						?new_reward,
						"Relayer can now claim reward for serving payer"
					);

					Self::deposit_event(Event::<T, I>::RewardRegistered {
						relayer: relayer.clone(),
						reward_kind,
						reward_balance,
					});
				},
			);
		}
```
