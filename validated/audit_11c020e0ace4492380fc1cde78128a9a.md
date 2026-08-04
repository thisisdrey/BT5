Based on my investigation, here is the confirmed local analog to the H-17 "fee burned on wrong account" bug class.

### Title
Snowbridge reward `pay_reward` withdraws XCM delivery fee from the claiming relayer's own account on Bridge Hub instead of from the WETH reward being minted, causing legitimate reward claims to fail or drain unrelated balances - ([File: bridges/snowbridge/primitives/core/src/reward.rs])

### Summary
`PayAccountOnLocation::pay_reward` builds an XCM that mints the relayer's WETH reward on AssetHub, then charges the *local* delivery fee for sending that XCM by calling `XcmExecutor::charge_fees(relayer.clone(), fee)` [1](#0-0) . This withdraws the fee from the relayer's own native-currency balance on Bridge Hub — an account that is completely unrelated to the WETH reward value being transferred and is not guaranteed to hold any spendable balance, exactly mirroring the Vader bug where a fee is burned from an address (`msg.sender`/`relayer`) that is not actually a party to the value being moved.

### Finding Description
`claim_rewards` / `claim_rewards_to` in `pallet_bridge_relayers` let any registered relayer claim an arbitrarily large accumulated WETH reward and send it to an arbitrary `beneficiary` location on AssetHub [2](#0-1) . The dispatch flows into `BridgeRewardPayer::pay_reward` for `BridgeReward::Snowbridge`, which calls `snowbridge_core::reward::PayAccountOnLocation::pay_reward(relayer, (), reward, account_location)` [3](#0-2) .

Inside `pay_reward`, the reward itself (WETH) is entirely encoded as a `ReserveAssetDeposited` inside the outgoing XCM to AssetHub — it never touches the relayer's local balance. However, the *delivery fee* for sending that outgoing message is charged separately, by directly withdrawing from `relayer`'s account via `XcmExecutor::charge_fees(relayer.clone(), fee)` [4](#0-3) . `charge_fees` in the XCM executor performs an unconditional `AssetTransactor::withdraw_asset` from the given origin/location [5](#0-4) , i.e. it debits the relayer's native token balance on Bridge Hub — an account whose only expected balance state is "whatever it happens to hold," not something tied to the reward-claim transaction's value flow.

This is structurally identical to the Vader `H-17` pattern: the fee-bearing account (`relayer`/`msg.sender`) is not the account that is the actual subject of the value transfer (the WETH reward going to `beneficiary` on AssetHub). Any relayer whose Bridge Hub native balance is empty or below the delivery-fee amount — which is entirely plausible for a relayer that operates purely off-chain and only interacts with Bridge Hub through this reward-claim flow — will have their claim revert with `ChargeFeesFailure`, permanently locking their accumulated reward in the `RelayerRewards` map, since `do_claim_rewards` uses `try_mutate_exists` and only calls `take()` on the reward entry after the whole payment procedure would need to succeed [6](#0-5) . Because `pay_reward` returns an error before the reward map entry is removed, this specific failure mode does not duplicate payouts, but it deterministically locks the reward until the relayer separately funds an account that has nothing to do with the reward being claimed — effectively a permanent-lock condition triggerable by an unprivileged relayer simply by having ever claimed nothing else on Bridge Hub.

### Impact Explanation
This falls under "permanent user-fund or bridge-state lock": a legitimate relayer's accrued WETH reward becomes unclaimable through the normal path because an unrelated fee-paying account (their Bridge Hub native balance) lacks funds, even though the reward itself is fully backed and ready to be minted on AssetHub. It does not require a malicious actor, governance, or privileged access — it is a direct consequence of normal usage by an honest relayer who has not separately funded a DOT/native balance on Bridge Hub.

### Likelihood Explanation
Likelihood is moderate-to-high: relayers are explicitly permissionless, unprivileged actors (per the bridges reward docs, "anyone may start its own relayer") [7](#0-6) , and nothing in the reward-registration or claim flow requires the relayer to already hold native Bridge Hub balance before accumulating rewards. A relayer whose entire operational balance is denominated in WETH/ETH on the Ethereum side (per Snowbridge's design intent of "users pay remote delivery costs in ETH") [8](#0-7)  would predictably hit this on their very first claim.

### Recommendation
Charge the local XCM delivery fee for the reward-minting message from the reward amount itself (deduct it from `reward`/`assets` before constructing the `ReserveAssetDeposited` in `pay_reward`), or use `jit_withdraw`/fees-from-holding semantics so the fee is paid out of the value being transferred rather than debited from the relayer's unrelated local balance. Alternatively, ensure `claim_rewards`/`claim_rewards_to` fail fast with a clear, refundable error and do not lock the reward when `ChargeFeesFailure` occurs, and/or expose a mechanism that lets a relayer claim without needing pre-existing Bridge Hub native balance.

### Proof of Concept
1. A relayer accumulates a Snowbridge WETH reward via `process_delivery_receipt` calling `T::RewardPayment::register_reward(&reward_account, ...)` [9](#0-8) , recorded in `RelayerRewards`.
2. This relayer has zero (or insufficient) native token balance on Bridge Hub, which is realistic since their sole interaction with the chain has been submitting Ethereum message proofs.
3. The relayer calls `claim_rewards_to(origin, BridgeReward::Snowbridge, beneficiary)` [2](#0-1) .
4. `pay_reward` builds the mint-XCM and calls `XcmExecutor::charge_fees(relayer.clone(), fee)` [1](#0-0) , which fails with `ChargeFeesFailure` because the relayer's Bridge Hub balance cannot cover `fee`.
5. `do_claim_rewards` returns `Error::<T, I>::FailedToPayReward` and the reward entry is never removed from `RelayerRewards`, leaving the WETH reward permanently unclaimable until the relayer funds an account unrelated to the reward transaction itself.

### Citations

**File:** bridges/snowbridge/primitives/core/src/reward.rs (L127-151)
```rust
	fn pay_reward(
		relayer: &Relayer,
		_: (),
		reward: RewardBalance,
		beneficiary: Self::Beneficiary,
	) -> Result<(), Self::Error> {
		let ethereum_location = Location::new(2, [GlobalConsensus(EthereumNetwork::get())]);
		let assets: Asset = (ethereum_location.clone(), reward.into()).into();

		let xcm: Xcm<()> = alloc::vec![
			UnpaidExecution { weight_limit: Unlimited, check_origin: None },
			DescendOrigin(InboundQueueLocation::get().into()),
			UniversalOrigin(GlobalConsensus(EthereumNetwork::get())),
			ReserveAssetDeposited(assets.into()),
			DepositAsset { assets: AllCounted(1).into(), beneficiary },
		]
		.into();

		let (ticket, fee) =
			validate_send::<XcmSender>(AssetHubLocation::get(), xcm).map_err(|_| XcmSendFailure)?;
		XcmExecutor::charge_fees(relayer.clone(), fee).map_err(|_| ChargeFeesFailure)?;
		XcmSender::deliver(ticket).map_err(|_| XcmSendFailure)?;

		Ok(())
	}
```

**File:** bridges/modules/relayers/src/lib.rs (L224-235)
```rust
		/// Claim accumulated rewards and send them to the alternative beneficiary.
		#[pallet::call_index(3)]
		#[pallet::weight(T::WeightInfo::claim_rewards_to())]
		pub fn claim_rewards_to(
			origin: OriginFor<T>,
			reward_kind: T::Reward,
			beneficiary: BeneficiaryOf<T, I>,
		) -> DispatchResult {
			let relayer = ensure_signed(origin)?;

			Self::do_claim_rewards(relayer, reward_kind, beneficiary)
		}
```

**File:** bridges/modules/relayers/src/lib.rs (L263-302)
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

**File:** cumulus/parachains/runtimes/bridge-hubs/bridge-hub-westend/src/bridge_common_config.rs (L117-136)
```rust
			BridgeReward::Snowbridge => {
				match beneficiary {
					BridgeRewardBeneficiaries::LocalAccount(_) => Err(Self::Error::Other("`LocalAccount` beneficiary is not supported for `Snowbridge` rewards!")),
					BridgeRewardBeneficiaries::AssetHubLocation(account_location) => {
						let account_location = Location::try_from(account_location)
							.map_err(|_| Self::Error::Other("`AssetHubLocation` beneficiary location version is not supported for `Snowbridge` rewards!"))?;
						snowbridge_core::reward::PayAccountOnLocation::<
							AccountId,
							u128,
							EthereumNetwork,
							AssetHubLocation,
							InboundQueueV2Location,
							XcmRouter,
							XcmExecutor<XcmConfig>,
							RuntimeCall
						>::pay_reward(
							relayer, (), reward, account_location
						)
					}
				}
```

**File:** polkadot/xcm/xcm-executor/src/lib.rs (L319-330)
```rust
	fn charge_fees(origin: impl Into<Location>, fees: Assets) -> XcmResult {
		let origin = origin.into();
		if !Config::FeeManager::is_waived(Some(&origin), FeeReason::ChargeFees) {
			let mut charged = AssetsInHolding::new();
			for asset in fees.inner() {
				let withdrawn = Config::AssetTransactor::withdraw_asset(&asset, &origin, None)?;
				charged.subsume_assets(withdrawn);
			}
			Config::FeeManager::handle_fee(charged, None, FeeReason::ChargeFees);
		}
		Ok(())
	}
```

**File:** bridges/docs/polkadot-kusama-bridge-overview.md (L41-44)
```markdown
There's no any active relayer sets, or something like that. Anyone may start its own relayer and relay queued messages.
We are not against that and, as always, appreciate any community efforts. Of course, running relayer has the cost. Apart
from paying for the CPU and network, the relayer pays for transactions at both sides of the bridge. We have a mechanism
for rewarding relayers.
```

**File:** bridges/snowbridge/docs/v2.md (L246-249)
```markdown
### Step 2: Relayer relays message to Bridge Hub

On-chain exchange rate is eliminated. Users pay remote delivery costs in ETH, and this amount is sent with the message
as WETH. The delivery fee can be claimed by the relayer on BH.
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L462-473)
```rust
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
```
