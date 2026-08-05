## Analysis

The Nudge bug's core broken invariant is: **settlement finality on the source side is granted based on an address that may not resolve to a controllable account on the destination side**, permanently losing user funds. The exact structural analog exists in `pallet-bridge-relayers`' Snowbridge reward-claim flow.

`claim_rewards_to` is a public, unprivileged extrinsic that lets *any* relayer who has an accrued reward choose an arbitrary destination `Location` beneficiary for a cross-chain (BridgeHub → AssetHub) payout, and the pallet irreversibly clears the reward balance as soon as the *local* XCM send succeeds — with no confirmation that the beneficiary actually resolves to an account the relayer controls on AssetHub.

### Title
Snowbridge relayer rewards are permanently burned when `claim_rewards_to` sends the payout XCM to an unresolvable/uncontrolled `AssetHubLocation` beneficiary - (File: bridges/snowbridge/primitives/core/src/reward.rs)

### Summary
`pallet_bridge_relayers::claim_rewards_to` clears the caller's accrued reward from storage as soon as `PaymentProcedure::pay_reward` returns `Ok`. For Snowbridge rewards, `pay_reward` is implemented by `PayAccountOnLocation::pay_reward`, which builds an `UnpaidExecution` XCM containing `DepositAsset { .. , beneficiary }` using the caller-supplied `Location` and fires it at AssetHub via `XcmSender::deliver`. Success here only means "the XCM was accepted for delivery on BridgeHub" — it says nothing about whether the `beneficiary` location will resolve to an account the relayer actually controls on AssetHub, or whether the remote `DepositAsset` will even succeed.

### Finding Description [1](#0-0) 
`do_claim_rewards` takes the reward out of `RelayerRewards` storage and, if `PaymentProcedure::pay_reward` returns `Ok`, permanently emits `RewardPaid` and never restores the balance. [2](#0-1) 
`PayAccountOnLocation::pay_reward` takes `beneficiary: Location` directly from the caller (via `BridgeRewardBeneficiaries::AssetHubLocation(VersionedLocation)` in `claim_rewards_to`), builds a fire-and-forget `UnpaidExecution` XCM with `DepositAsset { assets: AllCounted(1).into(), beneficiary }`, and returns `Ok(())` once `XcmSender::deliver` succeeds — i.e. once the message merely enters the outbound queue to AssetHub. [3](#0-2) 
`BridgeRewardPayer::pay_reward` routes `BridgeReward::Snowbridge` claims straight into `PayAccountOnLocation` with the relayer-supplied `account_location`, with no validation that the `Location` decodes to an account controllable via a signed origin on AssetHub (e.g. correct `network` tag, valid `AccountId32`/`AccountKey20` junction, not a bare parachain/pallet location, etc.).

Just as the Nudge campaign assumed `userAddress` (Ethereum mainnet) was equally valid on the destination chain (Base) for account-abstraction wallets, `claim_rewards_to` assumes a caller-chosen `Location` on BridgeHub is meaningful and claimable as-is on AssetHub. If the beneficiary is malformed relative to what AssetHub's `LocationToAccountId`/signed-origin converter would actually recognize as belonging to the relayer (wrong `NetworkId`, wrong junction structure, a location aliasing to nobody's signed origin), `DepositAsset` on AssetHub either deposits into an account nobody can sign for, or fails and traps the assets against a claimer nobody controls (the same trapped-asset/`AssetsTrapped` mechanism already shown to be fragile for the *inbound* path, cf. the `pr_11919` fix for the analogous default-claimer network mismatch). Either way, the reward is gone: BridgeHub already deleted `RelayerRewards` and fired `RewardPaid` before any of this remote outcome is known.

### Impact Explanation
This is a permanent, unbacked loss of relayer reward funds — direct value loss with no recovery path, matching the "permanent user-fund or bridge-state lock" and "duplicate settlement or payout" categories in the impact gate. It is triggerable by an ordinary unprivileged relayer account (no malicious peer, validator, governance, or leaked key required) purely by supplying a beneficiary `Location` whose account-derivation semantics differ between BridgeHub and AssetHub — exactly the account-abstraction-style cross-chain address mismatch in the seed report.

### Likelihood Explanation
Likelihood is non-trivial: relayer tooling/UIs constructing the `VersionedLocation` beneficiary must get the `NetworkId`/junction encoding exactly right for AssetHub's signed-origin converter (`SignedToAccountId32<_, _, LocalNetwork>`), the same class of mismatch that was already identified and had to be patched for the *inbound* claimer fallback path (`pr_11919`). No on-chain guard exists to prevent the same mismatch on this *outbound* reward-claim path — settlement (clearing the reward) is not made contingent on remote execution succeeding.

### Recommendation
Do not clear `RelayerRewards` until remote settlement is confirmed, or restrict `AssetHubLocation` beneficiaries to a validated form (e.g. require it to decode via the same converter AssetHub uses for signed origins, or require the relayer to prove ownership by executing the claim as an XCM round trip / query-based confirmation) before treating the reward as paid. At minimum, validate that `Location` contains a recognizable `AccountId32`/`AccountKey20` junction with the correct `NetworkId` before accepting it in `claim_rewards_to`.

### Proof of Concept
1. Relayer accrues a `BridgeReward::Snowbridge` reward on BridgeHub via `register_reward` (as in `bridge_rewards_works` test). [4](#0-3) 
2. Relayer calls `claim_rewards_to(BridgeReward::Snowbridge, BridgeRewardBeneficiaries::AssetHubLocation(location))` where `location` is an `AccountId32` junction with `network: None` (or any encoding that does not match what AssetHub's signed-origin converter would produce for any account the relayer controls).
3. `PayAccountOnLocation::pay_reward` builds and delivers the `DepositAsset` XCM to AssetHub and returns `Ok`; `do_claim_rewards` clears the reward entry and emits `RewardPaid`.
4. On AssetHub, `DepositAsset` either deposits into an account nobody can sign transactions from, or fails/traps assets against a location the relayer cannot present a matching signed origin for (mirroring the exact "network: None vs network: Some(LocalNetwork)" mismatch previously fixed only for the inbound fallback claimer in `prdoc/stable2603-3/pr_11919.prdoc`).
5. The relayer's reward is permanently gone — `RelayerRewards` cannot be restored and the AssetHub-side value cannot be reclaimed.

### Citations

**File:** bridges/modules/relayers/src/lib.rs (L263-301)
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
```

**File:** bridges/snowbridge/primitives/core/src/reward.rs (L125-151)
```rust
	type Beneficiary = Location;

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

**File:** cumulus/parachains/runtimes/bridge-hubs/bridge-hub-westend/src/bridge_common_config.rs (L117-135)
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
```

**File:** cumulus/parachains/runtimes/bridge-hubs/bridge-hub-westend/tests/tests.rs (L777-795)
```rust
			let claim_location = VersionedLocation::V5(Location::new(
				1,
				[
					Parachain(1000),
					xcm::latest::Junction::AccountId32 {
						id: account2.clone().into(),
						network: None,
					},
				],
			));
			// In unit tests without proper HRMP channel setup, the claim will fail at XCM sending.
			assert_err!(
				BridgeRelayers::claim_rewards_to(
					RuntimeOrigin::signed(account2.clone()),
					BridgeReward::Snowbridge,
					BridgeRewardBeneficiaries::AssetHubLocation(claim_location)
				),
				pallet_bridge_relayers::Error::<Runtime, BridgeRelayersInstance>::FailedToPayReward
			);
```
