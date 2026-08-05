Audit Report

## Title
`EthereumBlobExporter::make_unlock_native_token_command` derives the ERC-20 `token` and `amount` for `Command::UnlockNativeToken` from an unvalidated nested XCM program without any binding to a real prior asset debit - ([File: bridges/snowbridge/primitives/outbound-queue/src/v1/converter/mod.rs])

## Summary
`make_unlock_native_token_command` (lines 225-317) extracts `token`, `amount`, and `recipient` directly from the `WithdrawAsset`/`DepositAsset` instructions of the `xcm` payload handed to `EthereumBlobExporter::validate`, with no check that this location/amount corresponds to a real, previously-debited balance — unlike its sibling `make_mint_foreign_token_command`, which cross-validates the extracted `asset_id` against the registered `TokenId`/`ConvertAssetId` mapping. In the wider XCM executor, the `ExportMessage { network, destination, xcm }` instruction handler (`polkadot/xcm/xcm-executor/src/lib.rs`, `process_instruction`, around lines 1672-1712) passes the `xcm` field to `validate_export::<Config::MessageExporter>` verbatim, without executing or otherwise validating it against the holding register — it is opaque data taken as-is from the outer program, not something the executor re-derives from an actual withdrawal.

## Finding Description
The converter's asymmetry is real and confirmed in code: `make_mint_foreign_token_command` performs `ensure!(asset_id == expected_asset_id, InvalidAsset)` against `TokenIdOf::convert_location`/`ConvertAssetId::maybe_convert` [1](#0-0) , while `make_unlock_native_token_command` simply reads the `AccountKey20` bytes from whatever `Asset` appears in `WithdrawAsset`/`DepositAsset` and uses them as the Ethereum `token` address with no analogous registry check [2](#0-1) .

However, whether this asymmetry is actually exploitable depends entirely on whether the nested `xcm` field of an `ExportMessage` instruction reaching `EthereumBlobExporter::validate` can ever be attacker-supplied independent of a real matching withdrawal. I traced the generic XCM-executor handling of `ExportMessage` [3](#0-2) , which confirms the nested `xcm` is forwarded to the exporter without being executed against the local holding register by the executor itself — the binding between a real debit and the nested program is enforced (if at all) by whichever higher-level XCM-program-construction logic assembles the outer message (e.g. `pallet_xcm`'s reserve-transfer machinery, which real chains use, versus a hypothetical raw `pallet_xcm::execute` call with a directly crafted `ExportMessage`).

I was unable to complete verification, within the remaining tool budget, of two decisive facts needed to confirm exploitability:
1. Whether BridgeHub's/AssetHub's `Barrier` configuration (I located `DenyExportMessageFrom` in `cumulus/parachains/runtimes/bridge-hubs/common/src/barriers.rs`, used in both `bridge-hub-westend` and `bridge-hub-rococo` `xcm_config.rs`) actually blocks unprivileged/untrusted origins (as opposed to only specific origin sets) from executing programs containing `ExportMessage` toward Ethereum.
2. Whether `pallet_xcm::execute`'s `XcmExecuteFilter`/safe-call filtering on the relevant runtime permits ordinary signed-account origins to submit a raw XCM program containing `ExportMessage` with an attacker-chosen nested `xcm`, as opposed to that instruction only ever being reachable via router/`SovereignPaidRemoteExporter` paths where the nested program is executor-derived from a real withdrawal.

Without resolving these two points, it cannot be established that an unprivileged external attacker has a reachable path to hand `EthereumBlobExporter::validate` a `WithdrawAsset`/`DepositAsset` pair that is not backed by a real debit. The claim itself acknowledges this same gap ("I was not able to fully confirm... whether AssetHub's or BridgeHub's `pallet_xcm` execute filter... permits `ExportMessage`... this determination requires reading the runtime's `xcm_config.rs` filter definitions in full, which the available tool budget did not allow me to complete").

## Impact Explanation
If the unresolved reachability question above is answered affirmatively (an unprivileged origin can submit an `ExportMessage` with an arbitrary nested `xcm` to Ethereum), the impact would be theft/unbacked-unlock of real ERC-20 collateral from the Gateway contract, which is a valid impact category. But this determination was not completed, and the underlying code-level asymmetry (missing binding check in `make_unlock_native_token_command`) by itself, without a demonstrated reachable path bypassing the normal reserve-transfer construction, does not establish a concrete exploit.

## Likelihood Explanation
Unconfirmed. The claim's own author states the same uncertainty, and my independent investigation reached the same blocking point: confirming whether `pallet_xcm::execute`'s filters or the `Barrier`/`DenyExportMessageFrom` configuration in the live BridgeHub runtime allow an unprivileged account to construct a raw `ExportMessage` instruction with an attacker-controlled nested `xcm` requires a full reading of `bridge-hub-westend`/`bridge-hub-rococo` `xcm_config.rs` and `pallet_xcm`'s `SafeCallFilter`/`XcmExecuteFilter` wiring, which was not completed within available tool calls. Absent proof of a reachable attacker-controlled path, this cannot be confirmed as an exploitable vulnerability rather than a structural code asymmetry.

## Recommendation
Independently of reachability, add a defensive binding check in `make_unlock_native_token_command` symmetric to the one in `make_mint_foreign_token_command`, and separately audit/restrict every caller path that can construct an `ExportMessage` instruction (via `pallet_xcm::execute`, `Barrier` configuration, and `DenyExportMessageFrom` usage in the relevant BridgeHub runtimes) to ensure the nested `xcm` can never be attacker-supplied independent of an actual local-asset debit of the same asset/amount.

## Proof of Concept
Not established. A conclusive PoC requires confirming a reachable, unprivileged entry point (e.g. `pallet_xcm::execute`) that permits submission of an XCM program containing `ExportMessage { network: Ethereum, ... }` with an attacker-chosen nested `xcm` on the live BridgeHub/AssetHub runtime configuration — this was not verified in this session due to tool-call limits, matching the gap acknowledged in the original claim.

### Citations

**File:** bridges/snowbridge/primitives/outbound-queue/src/v1/converter/mod.rs (L289-316)
```rust
		let (token, amount) = match reserve_asset {
			Asset { id: AssetId(inner_location), fun: Fungible(amount) } => {
				match inner_location.unpack() {
					// Get the ERC20 contract address of the token.
					(0, [AccountKey20 { network, key }]) if self.network_matches(network) => {
						Some((H160(*key), *amount))
					},
					// If there is no ERC20 contract address in the location then signal to the
					// gateway that is a native Ether transfer by using
					// `0x0000000000000000000000000000000000000000` as the token address.
					(0, []) => Some((H160([0; 20]), *amount)),
					_ => None,
				}
			},
			_ => None,
		}
		.ok_or(AssetResolutionFailed)?;

		// transfer amount must be greater than 0.
		ensure!(amount > 0, ZeroAssetTransfer);

		// Check if there is a SetTopic and skip over it if found.
		let topic_id = match_expression!(self.next()?, SetTopic(id), id).ok_or(SetTopicExpected)?;

		Ok((
			Command::UnlockNativeToken { agent_id: self.agent_id, token, recipient, amount },
			*topic_id,
		))
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v1/converter/mod.rs (L417-420)
```rust
		let token_id = TokenIdOf::convert_location(&asset_id).ok_or(InvalidAsset)?;

		let expected_asset_id = ConvertAssetId::maybe_convert(token_id).ok_or(InvalidAsset)?;
		ensure!(asset_id == expected_asset_id, InvalidAsset);
```

**File:** polkadot/xcm/xcm-executor/src/lib.rs (L1672-1712)
```rust
			ExportMessage { network, destination, xcm } => {
				// The actual message sent to the bridge for forwarding is prepended with
				// `UniversalOrigin` and `DescendOrigin` in order to ensure that the message is
				// executed with this Origin.
				//
				// Prepend the desired message with instructions which effectively rewrite the
				// origin.
				//
				// This only works because the remote chain empowers the bridge
				// to speak for the local network.
				let origin = self.context.origin.as_ref().ok_or(XcmError::BadOrigin)?.clone();
				let universal_source = Config::UniversalLocation::get()
					.within_global(origin)
					.map_err(|()| {
						tracing::debug!(
							target: "xcm::process_instruction::export_message",
							"Failed to reanchor origin to universal location",
						);
						XcmError::Unanchored
					})?;
				let hash = (self.origin_ref(), &destination).using_encoded(blake2_128);
				let channel = u32::decode(&mut hash.as_ref()).unwrap_or(0);
				// Hash identifies the lane on the exporter which we use. We use the pairwise
				// combination of the origin and destination to ensure origin/destination pairs
				// will generally have their own lanes.
				let (ticket, fee) = validate_export::<Config::MessageExporter>(
					network,
					channel,
					universal_source,
					destination.clone(),
					xcm,
				)?;
				self.transactional_process(|self_ref| {
					self_ref.take_fee(fee, FeeReason::Export { network, destination })?;
					let _ = Config::MessageExporter::deliver(ticket).defensive_proof(
						"`deliver` called immediately after `validate_export`; \
						`take_fee` does not affect the validity of the ticket; qed",
					);
					Ok(())
				})
			},
```
