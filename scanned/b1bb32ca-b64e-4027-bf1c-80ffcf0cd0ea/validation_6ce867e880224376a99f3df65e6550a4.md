## Analog identified: multi-hop XCM reserve transfers can strand funds in an intermediate sovereign account when a later hop's `BuyExecution` weight/fee is insufficient — with no automatic recovery path

### Title
Reserve assets can become permanently stranded in an intermediate chain's sovereign account when `DepositReserveAsset`'s onward notification message under-buys execution weight for a further hop - (File: `polkadot/xcm/xcm-executor/src/lib.rs`)

### Summary
The TON report's core defect is: an intermediate contract (`router2`) receives funds custodially, but the message that should trigger it to act on those funds is starved of gas (`fwd_ton_amount` zeroed), so the funds sit unclaimed forever. The structural analog in this repository is multi-hop XCM reserve transfers (`InitiateReserveWithdraw` → `DepositReserveAsset` chained across several parachains, exactly as exercised in `cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/asset_transfers.rs:906-951`, a genuine 3-hop "router-through-router" transfer). At each hop, the `DepositReserveAsset` instruction handler in the XCM executor **commits the asset custody transfer first**, then sends the onward `ReserveAssetDeposited` notification to the next hop, gated only by a caller-chosen `BuyExecution`/`weight_limit`. If that weight/fee budget is insufficient for the rest of the program at the next hop, the deposit at the current hop is already final and irrevocable, while the forwarding notification fails to complete downstream — stranding the underlying assets in a sovereign account with no compensating protocol-level recovery.

### Finding Description
In `polkadot/xcm/xcm-executor/src/lib.rs`, `DepositReserveAsset` is processed as: [1](#0-0) 

It calls `do_reserve_deposit_assets`, which performs the actual deposit into `dest`'s sovereign account via `Self::deposit_assets_with_retry(assets, dest, context)` and only afterward appends `ReserveAssetDeposited(...)` to the *onward* message that gets `self_ref.send(dest, Xcm(message), ...)`: [2](#0-1) 

The onward message that is sent onward embeds the caller-supplied `xcm` (built by `remote_reserve_transfer_program`/`transfer_assets_using_type_and_then`/hand-crafted `pallet_xcm::execute`), which begins with a `BuyExecution { fees: dest_fees, weight_limit }` whose sufficiency is *not* verified against the full downstream program at construction time: [3](#0-2) 

The real-world multi-hop pattern that maps 1:1 onto the "cross-router swap" scenario in the report is demonstrated by the repository's own bridge-hub-westend integration test, chaining `InitiateReserveWithdraw` through two intermediate reserve chains before a final `DepositReserveAsset`/`DepositAsset`, each gated by its own `BuyExecution`: [4](#0-3) 

If the `weight_limit`/fee amount chosen for an intermediate hop's `BuyExecution` (analogous to the user-supplied `fwd_ton_amount` in the TON report) turns out to be insufficient for that hop's remaining program (e.g., the nested `InitiateReserveWithdraw`/`DepositReserveAsset` plus everything after it), execution at that hop aborts partway through. Any assets already deposited into the *next* hop's sovereign account by an earlier `DepositReserveAsset` on a prior hop are not rolled back — that custody transfer already committed in a separate top-level XCM message/dispatch. Only assets still sitting in the *local* Holding register at the failing hop get trapped and are theoretically claimable via `pallet_xcm::claim_assets`: [5](#0-4) [6](#0-5) 

But `claim_assets` requires an extrinsic executed *from the exact origin Location* that was recorded as claimer — for a cross-chain custody transfer, that is effectively the sending parachain's own XCM origin, not any ordinary end user, and it cannot reach back to recover value that is already resident as a plain balance increase in a sovereign account on an *earlier* hop (that balance is indistinguishable from a normal top-up; there is no trap/claim mechanism for reserve deposits that already fully executed on a prior hop). This exactly mirrors the TON bug's invariant break: "custody transfer commits unconditionally; the continuation notification that should act on it is gated by an under-funded weight/fee parameter, and once that notification fails to complete, the funds are stuck with no protocol-native path back to the beneficiary."

### Impact Explanation
Funds sent via chained reserve transfers (a supported, documented multi-hop pattern: `remote_reserve_transfer_program`, `transfer_assets_using_type_and_then`, or manually constructed `pallet_xcm::execute`/`send`) can become permanently locked in an intermediate parachain's sovereign account if the weight/fee budget provided for a downstream hop's `BuyExecution` is insufficient for that hop's full remaining program. This is a "permanent user-fund lock" outcome reachable through public, unprivileged extrinsics (`pallet_xcm::send`, `pallet_xcm::execute`, `limited_reserve_transfer_assets`, `transfer_assets_using_type_and_then`), with no admin/governance/relayer misbehavior required.

### Likelihood Explanation
The likelihood is non-trivial: multi-hop reserve transfers with per-hop fee-splitting are the repository's own supported design (see `halve_fees`/`remote_reserve_transfer_program`), and the weight/fee required by a nested program at a *downstream* hop is not mechanically checked against the `weight_limit` chosen at message-construction time on the *origin* chain — exactly the same "the sender guesses gas/weight per hop and there's no consistency check" root cause that the TON report calls out. Any user composing a manual multi-hop XCM (as the bridge-hub-westend test itself does, deliberately, for legitimate use) who under-specifies an intermediate `BuyExecution` budget can trigger this.

### Recommendation
- Require verification, at message-construction time on the origin chain (or via a dry-run/weighing pass across the whole intended multi-hop program), that each hop's `BuyExecution`/`weight_limit` is sufficient for that hop's full downstream program before allowing the top-level dispatch to proceed.
- Alternatively, make `DepositReserveAsset`'s custody transfer transactional across the full multi-hop chain (e.g., via a callback/rollback protocol) so that a downstream failure can trigger a refund message back through the chain to the original beneficiary/refund address, rather than leaving the deposited assets inert in an intermediate sovereign account.
- Consider extending the `AssetTrap`/`claim_assets` mechanism (or an equivalent) to cover assets that are custodially resident in a sovereign account as the result of a completed but not-fully-propagated multi-hop reserve deposit, giving users a documented recovery path.

### Proof of Concept
1. Construct a 3-location reserve-transfer chain exactly as in the repository's own test (`cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/asset_transfers.rs:915-951`): OriginPara → Reserve1 (`InitiateReserveWithdraw`) → Reserve2 (`InitiateReserveWithdraw`) → FinalDest (`DepositReserveAsset`/`DepositAsset`).
2. At the Reserve2 hop, set the `BuyExecution { fees, weight_limit }` supplied for the nested program (the one that will run at Reserve2, i.e., the `DepositReserveAsset` + final `xcm` to FinalDest) to a `Limited` value that is smaller than the true weight required by the full downstream `DepositReserveAsset`+`DepositAsset` program.
3. Submit this program via `pallet_xcm::execute` (or via `send`) from OriginPara. Reserve1's leg succeeds normally, depositing/forwarding to Reserve2. At Reserve2, `BuyExecution` (using the intentionally-underfunded fee) followed by `DepositReserveAsset` executes far enough to move assets into FinalDest's sovereign account on Reserve2 and dispatch the onward `ReserveAssetDeposited` message — but the remaining instructions in the program on Reserve2 (or subsequently at FinalDest, receiving a message whose accompanying `BuyExecution` was similarly underfunded) exhaust the weight/fee budget and abort.
4. Observe: the transferred asset value now sits as a plain balance increase in FinalDest's sovereign account *on Reserve2*, but the final beneficiary at FinalDest never receives a completed `DepositAsset`. No `claim_assets` call from an ordinary user account can recover it, since the claimer registered by `AssetTrap` corresponds only to locally-trapped Holding-register assets at whichever hop's execution aborted — not to the already-committed sovereign-account balance transfer that happened one hop earlier.

### Citations

**File:** polkadot/xcm/xcm-executor/src/lib.rs (L407-422)
```rust
		if !self.holding.is_empty() {
			tracing::trace!(
				target: "xcm::post_process",
				holding_register = ?self.holding,
				context = ?self.context,
				original_origin = ?self.original_origin,
				"Trapping assets in holding register",
			);
			let claimer = self
				.asset_claimer
				.as_ref()
				.or(self.context.origin.as_ref())
				.unwrap_or(&self.original_origin);
			let trap_weight = Config::AssetTrap::drop_assets(claimer, self.holding, &self.context);
			weight_used.saturating_accrue(trap_weight);
		};
```

**File:** polkadot/xcm/xcm-executor/src/lib.rs (L751-762)
```rust
	fn do_reserve_deposit_assets(
		assets: AssetsInHolding,
		dest: &Location,
		remote_xcm: &mut Vec<Instruction<()>>,
		context: Option<&XcmContext>,
	) -> Result<Assets, XcmError> {
		let reanchored_assets = Self::reanchored_assets(&assets, dest);
		Self::deposit_assets_with_retry(assets, dest, context)?;
		remote_xcm.push(ReserveAssetDeposited(reanchored_assets.clone()));

		Ok(reanchored_assets)
	}
```

**File:** polkadot/xcm/xcm-executor/src/lib.rs (L1203-1233)
```rust
			DepositReserveAsset { assets, dest, xcm } => {
				self.transactional_process(|self_ref| {
					let mut assets = self_ref.holding.saturating_take(assets);
					// When not using `PayFees`, nor `JIT_WITHDRAW`, delivery fees are paid from
					// transferred assets.
					let maybe_delivery_fee_from_assets = if self_ref.fees.is_empty() && !self_ref.fees_mode.jit_withdraw {
						// Deduct and return the part of `assets` that shall be used for delivery fees.
						self_ref.take_delivery_fee_from_assets(&mut assets, &dest, FeeReason::DepositReserveAsset, &xcm)?
					} else {
						None
					};
					let mut message = Vec::with_capacity(xcm.len() + 2);
					tracing::trace!(target: "xcm::DepositReserveAsset", ?assets, "Assets except delivery fee");
					Self::do_reserve_deposit_assets(
						assets,
						&dest,
						&mut message,
						Some(&self_ref.context),
					)?;
					// clear origin for subsequent custom instructions
					message.push(ClearOrigin);
					// append custom instructions
					message.extend(xcm.0.into_iter());
					if let Some(delivery_fee) = maybe_delivery_fee_from_assets {
						// Put back delivery_fee in holding register to be charged by XcmSender.
						self_ref.holding.subsume_assets(delivery_fee);
					}
					self_ref.send(dest, Xcm(message), FeeReason::DepositReserveAsset)?;
					Ok(())
				})
			},
```

**File:** polkadot/xcm/pallet-xcm/src/lib.rs (L1520-1573)
```rust
		/// Claims assets trapped on this pallet because of leftover assets during XCM execution.
		///
		/// - `origin`: Anyone can call this extrinsic.
		/// - `assets`: The exact assets that were trapped. Use the version to specify what version
		/// was the latest when they were trapped.
		/// - `beneficiary`: The location/account where the claimed assets will be deposited.
		#[pallet::call_index(12)]
		pub fn claim_assets(
			origin: OriginFor<T>,
			assets: Box<VersionedAssets>,
			beneficiary: Box<VersionedLocation>,
		) -> DispatchResult {
			let origin_location = T::ExecuteXcmOrigin::ensure_origin(origin)?;
			tracing::debug!(target: "xcm::pallet_xcm::claim_assets", ?origin_location, ?assets, ?beneficiary);
			// Extract version from `assets`.
			let assets_version = assets.identify_version();
			let assets: Assets = (*assets).try_into().map_err(|()| {
				tracing::debug!(
					target: "xcm::pallet_xcm::claim_assets",
					"Failed to convert input VersionedAssets",
				);
				Error::<T>::BadVersion
			})?;
			let number_of_assets = assets.len() as u32;
			let beneficiary: Location = (*beneficiary).try_into().map_err(|()| {
				tracing::debug!(
					target: "xcm::pallet_xcm::claim_assets",
					"Failed to convert beneficiary VersionedLocation",
				);
				Error::<T>::BadVersion
			})?;
			let ticket: Location = GeneralIndex(assets_version as u128).into();
			let mut message = Xcm(vec![
				ClaimAsset { assets, ticket },
				DepositAsset { assets: AllCounted(number_of_assets).into(), beneficiary },
			]);
			let weight = T::Weigher::weight(&mut message, Weight::MAX).map_err(|error| {
				tracing::debug!(target: "xcm::pallet_xcm::claim_assets", ?error, "Failed to calculate weight");
				Error::<T>::UnweighableMessage
			})?;
			let mut hash = message.using_encoded(sp_io::hashing::blake2_256);
			let outcome = T::XcmExecutor::prepare_and_execute(
				origin_location,
				message,
				&mut hash,
				weight,
				weight,
			);
			outcome.ensure_complete().map_err(|error| {
				tracing::error!(target: "xcm::pallet_xcm::claim_assets", ?error, "XCM execution failed with error");
				Error::<T>::LocalExecutionIncompleteWithError { index: error.index, error: error.error.into()}
			})?;
			Ok(())
		}
```

**File:** polkadot/xcm/pallet-xcm/src/lib.rs (L2618-2634)
```rust
		// xcm to be executed at dest
		let mut xcm_on_dest =
			Xcm(vec![BuyExecution { fees: dest_fees, weight_limit: weight_limit.clone() }]);
		// Use custom XCM on remote chain, or just default to depositing everything to beneficiary.
		let custom_xcm_on_dest = match beneficiary {
			Either::Right(custom_xcm) => custom_xcm,
			Either::Left(beneficiary) => {
				// deposit all remaining assets in holding to `beneficiary` location
				Xcm(vec![DepositAsset { assets: Wild(AllCounted(max_assets)), beneficiary }])
			},
		};
		xcm_on_dest.0.extend(custom_xcm_on_dest.into_iter());
		// xcm to be executed on reserve
		let xcm_on_reserve = Xcm(vec![
			BuyExecution { fees: reserve_fees, weight_limit },
			DepositReserveAsset { assets: Wild(AllCounted(max_assets)), dest, xcm: xcm_on_dest },
		]);
```

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/asset_transfers.rs (L915-951)
```rust
		let xcm = Xcm::<()>(vec![
			WithdrawAsset((roc_at_westend_parachains.clone(), amount).into()),
			SetFeesMode { jit_withdraw: true },
			InitiateReserveWithdraw {
				assets: Wild(AllCounted(1)),
				reserve: local_asset_hub,
				// executes on Westend Asset Hub
				xcm: Xcm::<()>(vec![
					BuyExecution {
						fees: (roc_at_westend_parachains.clone(), amount / 2).into(),
						weight_limit: Unlimited,
					},
					InitiateReserveWithdraw {
						assets: Wild(AllCounted(1)),
						reserve: asset_hub_rococo_location(),
						// executes on Rococo Asset Hub
						xcm: Xcm::<()>(vec![
							BuyExecution {
								fees: (roc_at_rococo_parachains.clone(), amount / 2).into(),
								weight_limit: Unlimited,
							},
							DepositReserveAsset {
								assets: Wild(AllCounted(1)),
								dest: AssetHubRococo::sibling_location_of(PenpalA::para_id()),
								// executes on Rococo Penpal
								xcm: Xcm::<()>(vec![
									BuyExecution {
										fees: (roc_at_rococo_parachains.clone(), amount / 2).into(),
										weight_limit: Unlimited,
									},
									DepositAsset { assets: Wild(AllCounted(1)), beneficiary },
								]),
							},
						]),
					},
				]),
			},
```
