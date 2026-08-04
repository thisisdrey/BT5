## Analysis Summary

Investigating the report's core broken invariant — **an accounting value ("how much should be paid out") is trusted and later settled without ever being checked against real, conserved backing** — the strongest local analog in this repository is in the Snowbridge reward-crediting/payout pipeline (`pallet-bridge-relayers` + `snowbridge-pallet-outbound-queue-v2` + `snowbridge_core::reward::PayAccountOnLocation`).

### Title
Unbacked reward crediting/payout for Snowbridge relayer rewards — reward ledger value is settled via XCM `ReserveAssetDeposited` with no conservation check against real backing - ([File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs], [File: bridges/snowbridge/primitives/core/src/reward.rs])

### Summary
`Pallet::process_delivery_receipt` unconditionally credits a relayer's claimable reward ledger with `order.fee` (a value carried inside the outbound `Message` since message submission) via `T::RewardPayment::register_reward`. When that ledger entry is later claimed through `claim_rewards_to`, the `Snowbridge` reward kind is paid out by `PayAccountOnLocation::pay_reward`, which does **not** transfer from any funded, balance-checked source account. Instead it builds an XCM containing a raw `ReserveAssetDeposited` instruction for the claimed amount and sends it to AssetHub, which mints/deposits that many synthetic Ether-denominated foreign assets to the beneficiary. Unlike the sibling `PayRewardFromAccount` procedure — used for `RococoWestend` rewards — which performs a real `fungible::Mutate::transfer` from a dedicated, fundable `rewards_account` (and therefore can never pay out more than was actually deposited there), the Snowbridge path has no equivalent "does the pot actually contain this much value" check anywhere in the call chain.

### Finding Description
- `do_process_message` in `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs:346-443` decodes an inbound (to BridgeHub) `Message{ fee, .. }` and stores a `PendingOrder{ nonce, fee, block_number }` in `PendingOrders`. [1](#0-0) 
- On `submit_delivery_receipt` → `process_delivery_receipt`, the pallet fetches the order and, if `order.fee > 0`, calls `T::RewardPayment::register_reward(&reward_account, T::DefaultRewardKind::get(), order.fee)` — crediting the reward ledger with the exact `fee` value that was attached to the message at submission time, with no cross-check against any escrowed/locked asset balance on this chain: [2](#0-1) 
- `register_reward` in `pallet-bridge-relayers` simply increments a `RelayerRewards` double-map entry (`RelayerRewards::<T, I>::try_mutate` style storage) with no reference to a funded backing account. [3](#0-2) 
- At claim time, `claim_rewards_to` calls `T::PaymentProcedure::pay_reward`. For `BridgeReward::Snowbridge`, this dispatches to `snowbridge_core::reward::PayAccountOnLocation::pay_reward`, which does **not** perform any transfer from a real balance. It instead constructs an XCM program containing `ReserveAssetDeposited(assets.into())` for the full claimed `reward` value and a `DepositAsset` to the beneficiary, then sends it to AssetHub: [4](#0-3) 
- Contrast this with the `RococoWestend` reward kind, which uses `PayRewardFromAccount`, whose `pay_reward` performs an actual `T::transfer(&rewards_account, &beneficiary, reward, ...)` — meaning it is fundamentally rate-limited by the real balance of `rewards_account`, and cannot pay out more than has genuinely been deposited there: [5](#0-4) 
- `BridgeRewardPayer::pay_reward` in the BridgeHub Westend runtime wires exactly this dichotomy together, routing `Snowbridge` rewards to the unchecked `PayAccountOnLocation` path and `RococoWestend` rewards to the balance-checked `PayRewardFromAccount` path. [6](#0-5) 

The corrupted value is `order.fee` → `RelayerRewards` entry → `reward` parameter of `PayAccountOnLocation::pay_reward`: at no point in this chain is the value validated against an actual, conserved store of ETH-denominated value held or reserved by the bridge. Existing guards (`ensure!(T::GatewayAddress::get() == receipt.gateway, ...)`, nonce/PendingOrders existence check, verifier-halted check as shown in `poc_m1`) only validate that a message-delivery proof is legitimate and that an order exists — none of them validate that the `fee` value recorded in that order was itself derived from, or is bounded by, real value transferred/locked on the Ethereum side. This is structurally identical to the report's flaw: a distribution/payout amount (`_amount`/`fee`) is accepted and later paid out without validating it against the actual backing balance that should cover it.

### Impact Explanation
If the `fee` value embedded in an outbound v2 message is attacker/user-influenceable (it originates from the XCM `remote_fee_asset`/fee-related fields supplied when a user calls `PolkadotXcm::execute`/`send` to route a message to Ethereum, as seen throughout `snowbridge_v2_outbound.rs` tests), and is not tightly bound to funds actually collected/locked for that specific message, then a relayer (or a colluding message sender) can inflate `order.fee`, get any valid delivery receipt processed, and register an oversized reward. Claiming it mints synthetic ETH-denominated assets on AssetHub via `ReserveAssetDeposited`, i.e., an unbacked mint of bridge-representative funds. This falls squarely under "theft or unbacked mint" and "duplicate settlement or payout" in the impact gate, since the settlement mechanism does not conserve value between the fee promised and any escrowed backing.

### Likelihood Explanation
Moderate-to-uncertain: I was able to fully trace and verify the payout half of the vulnerability (register_reward → unconditional XCM `ReserveAssetDeposited` mint with no balance check), which is a clear structural inconsistency compared to the balance-checked `PayRewardFromAccount` path used elsewhere in the very same runtime. What I could **not** fully verify within the available search budget is the exact upstream bound on `fee` — i.e., whether `bridges/snowbridge/primitives/outbound-queue/src/v2/converter/mod.rs` and the exporter’s fee-calculation logic strictly cap/derive `fee` from an amount that was genuinely reserved/paid by the sender before enqueueing, or whether the value is attacker-settable within an already-executed XCM program (e.g., through `PayFees`/`InitiateTransfer` fee legs) without a corresponding lock of Ethereum-side value. This upstream fee-derivation logic needs direct code review to confirm whether the unchecked mint at payout time is actually reachable by an unprivileged party or whether it is fully constrained by prior fee-collection logic.

### Recommendation
Require `PayAccountOnLocation::pay_reward` (and the `BridgeReward::Snowbridge` payout path generally) to debit an on-chain, provably-funded reward pot/escrow account before authorizing the `ReserveAssetDeposited`/mint on AssetHub — mirroring the conservation guarantee already implemented by `PayRewardFromAccount` for `RococoWestend` rewards. Additionally, audit the fee-derivation path (`snowbridge-outbound-queue-primitives::v2` converter/exporter) to ensure `Message.fee` is strictly bound to value that was actually collected/locked at message-submission time, and add an explicit invariant check in `process_delivery_receipt`/`register_reward` that the credited reward amount can never exceed the sum of value genuinely reserved for that specific message/nonce.

### Proof of Concept
Not independently reproducible from the available context — the payout-side unchecked mint (`PayAccountOnLocation::pay_reward`) is confirmed by code inspection, but a full end-to-end PoC requires confirming that `Message.fee`/`order.fee` can be set by an unprivileged XCM sender independent of real value collection, which needs further review of `snowbridge-outbound-queue-primitives::v2::converter` and the fee-charging XCM instructions (`PayFees`/`InitiateTransfer`) used when constructing v2 outbound messages, which I could not fully trace within the available tool budget. A Devin session with full repository/test access is recommended to confirm the upstream fee-binding logic and construct a concrete extrinsic sequence.

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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L462-475)
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

			<PendingOrders<T>>::remove(nonce);
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

**File:** bridges/primitives/relayers/src/lib.rs (L175-188)
```rust
	fn pay_reward(
		_: &Relayer,
		reward_kind: RewardsAccountParams<LaneId>,
		reward: RewardBalance,
		beneficiary: Self::Beneficiary,
	) -> Result<(), Self::Error> {
		T::transfer(
			&Self::rewards_account(reward_kind),
			&beneficiary.into(),
			reward.into(),
			Preservation::Expendable,
		)
		.map(drop)
	}
```

**File:** cumulus/parachains/runtimes/bridge-hubs/bridge-hub-westend/src/bridge_common_config.rs (L91-139)
```rust
impl bp_relayers::PaymentProcedure<AccountId, BridgeReward, u128> for BridgeRewardPayer {
	type Error = sp_runtime::DispatchError;
	type Beneficiary = BridgeRewardBeneficiaries;

	fn pay_reward(
		relayer: &AccountId,
		reward_kind: BridgeReward,
		reward: u128,
		beneficiary: BridgeRewardBeneficiaries,
	) -> Result<(), Self::Error> {
		match reward_kind {
			BridgeReward::RococoWestend(lane_params) => {
				match beneficiary {
					BridgeRewardBeneficiaries::LocalAccount(account) => {
						bp_relayers::PayRewardFromAccount::<
							Balances,
							AccountId,
							LegacyLaneId,
							u128,
						>::pay_reward(
							&relayer, lane_params, reward, account,
						)
					},
					BridgeRewardBeneficiaries::AssetHubLocation(_) => Err(Self::Error::Other("`AssetHubLocation` beneficiary is not supported for `RococoWestend` rewards!")),
				}
			},
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
			}
		}
	}
```
