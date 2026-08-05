This confirms the analog: Ethereum-style transactions handled by `pallet-revive` (`EthExtraImpl::get_eth_extension`, used both in `substrate/frame/revive/dev-node/runtime/src/lib.rs:211` and the asset-hub-westend runtime at `cumulus/parachains/runtimes/assets/asset-hub-westend/src/lib.rs:1784`) explicitly set `frame_system::CheckMortality::from(sp_runtime::generic::Era::Immortal)` — i.e., Ethereum-originated calls into `pallet-revive` (including calls to precompiles like the asset-conversion swap precompile) are **immortal** and only bounded by `CheckNonce`, not by any block-window/deadline. This directly parallels the external report's root cause: a swap call can remain valid indefinitely (bounded only by nonce ordering) and be included by a block author at any later point after being broadcast, well after the price conditions the user intended have changed.

### Title
Ethereum-origin swap calls into the AssetConversion precompile have no execution deadline and are immortal by design - ([File: substrate/frame/asset-conversion/precompiles/src/lib.rs])

### Summary
`IAssetConversion::swapExactTokensForTokens` / `swapTokensForExactTokens` in `substrate/frame/asset-conversion/precompiles/src/lib.rs` expose `pallet-asset-conversion` swaps to Solidity/`pallet-revive` contracts with only an `amountOutMin`/`amountInMax` slippage bound and no `deadline` parameter. Combined with the fact that Ethereum-style (`eth_transact`) extrinsics processed by `pallet-revive` are configured with `frame_system::CheckMortality::from(Era::Immortal)` (`substrate/frame/revive/dev-node/runtime/src/lib.rs:211`, and equivalently in `cumulus/parachains/runtimes/assets/asset-hub-westend/src/lib.rs:1784`), a signed swap transaction has no time/block bound at all — it stays valid until its nonce is consumed, exactly the "no deadline" pattern flagged in the external report against `SmartVaultV3#swap()`.

### Finding Description
The precompile interface (`substrate/frame/asset-conversion/precompiles/src/lib.rs:56-85`) defines swap calls with `amountOutMin`/`amountInMax` but no `deadline`. Internally this just forwards to `pallet_asset_conversion::Pallet::swap_exact_tokens_for_tokens` / `swap_tokens_for_exact_tokens` (lines 305-319, 337-350), which likewise take no deadline (`substrate/frame/asset-conversion/src/lib.rs:527-573`).

Ordinarily, native Substrate extrinsics are bounded by `frame_system::CheckEra`/`CheckMortality`, limiting how long a signed transaction stays valid in the pool. But for transactions that arrive as Ethereum-style `eth_transact` payloads (the intended way contracts and EOAs interact with `pallet-revive`, including calling this precompile), `EthExtra::get_eth_extension` hardcodes `Era::Immortal` for the mortality check (`substrate/frame/revive/dev-node/runtime/src/lib.rs:196-218`; identical pattern in the Asset Hub runtime `cumulus/parachains/runtimes/assets/asset-hub-westend/src/lib.rs:1768-1799`). The only remaining gating mechanism is `CheckNonce`, which enforces ordering, not timing — a transaction with a not-yet-consumed nonce can sit in a mempool or an off-chain relayer's queue indefinitely and be replayed/broadcast by anyone (it's a fully signed, self-contained payload) into a block at any future point.

This reproduces the exact primitive from the report: the swap-executing transaction has no self-expiring bound, so it can be "maliciously executed at a later point" once its price context has changed — the sole guard is the caller-supplied `amountOutMin`/`amountInMax`, which (as in the original Solidity report) only bounds worst-case slippage but does not prevent stale execution itself.

### Impact Explanation
If `amountOutMin`/`amountInMax` is set loosely (common in wallet/dApp default UX, or when a contract computes it off pool state that's since moved), an attacker (any block-producing validator/collator with normal, non-privileged capability, or any actor who can delay inclusion of a publicly-broadcast signed transaction) can withhold and later include the swap once the pool price has drifted, extracting value from the swapper up to the slack in the min/max bound. In vault-like consumer contracts built on top of this precompile (mirroring the original report's SmartVault scenario), a stale swap execution at a worse price can push a leveraged position toward liquidation. This is a real value-loss/MEV primitive on Asset Hub's AMM exposed to `pallet-revive` contracts.

### Likelihood Explanation
Moderate. It requires no privileged access — only that a legitimately-signed, fully valid Ethereum-style transaction exist and be delayed/replayed by whoever controls its propagation/inclusion timing (a normal, permissionless capability of anyone relaying transactions or producing blocks), which is exactly the actor class the report itself targets (a "malicious miner"/block producer analog, not a protocol-privileged role). The severity is bounded by the tightness of `amountOutMin`/`amountInMax`, but nothing in the protocol itself prevents indefinite delay.

### Recommendation
Add an explicit `deadline` parameter to `swapExactTokensForTokens`/`swapTokensForExactTokens` in `substrate/frame/asset-conversion/precompiles/src/lib.rs` (and ideally to the underlying `pallet_asset_conversion` extrinsics), checked against `frame_system::Pallet::<T>::block_number()` (or a timestamp) at execution time, so a swap fails if executed after its intended window — independent of whatever transaction-extension mortality policy the surrounding runtime chooses for Ethereum-style transactions.

### Proof of Concept
1. User submits an `eth_transact` calling `swapExactTokensForTokens(path, amountIn, amountOutMin, sendTo, keepAlive)` on the AssetConversion precompile, with `amountOutMin` computed from the current pool reserves at submission time.
2. The transaction extension stack for this call uses `CheckMortality::from(Era::Immortal)` (see `substrate/frame/revive/dev-node/runtime/src/lib.rs:211`), so the transaction remains valid indefinitely; only `CheckNonce` gates it.
3. A block producer (or anyone relaying the raw signed payload) withholds inclusion while pool reserves shift due to other trades.
4. The transaction is included many blocks later; `Pallet::do_swap_exact_tokens_for_tokens` executes against the new, worse reserves — succeeding as long as the realized output is still ≥ `amountOutMin`, extracting the difference from the user versus the price they intended when signing. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** substrate/frame/asset-conversion/precompiles/src/lib.rs (L56-70)
```rust
	interface IAssetConversion {
		/// Swap an exact amount of input tokens for as many output tokens as possible.
		/// @param path Ordered list of SCALE-encoded asset identifiers defining the swap route.
		/// @param amountIn Exact amount of the first asset to swap.
		/// @param amountOutMin Minimum acceptable amount of the last asset to receive.
		/// @param sendTo Address to receive the output tokens.
		/// @param keepAlive If true, ensures the sender account stays above existential deposit.
		/// @return amountOut The amount of output tokens received.
		function swapExactTokensForTokens(
			bytes[] calldata path,
			uint256 amountIn,
			uint256 amountOutMin,
			address sendTo,
			bool keepAlive
		) external returns (uint256 amountOut);
```

**File:** substrate/frame/asset-conversion/precompiles/src/lib.rs (L289-319)
```rust
	fn swap_exact_tokens_for_tokens(
		call: &IAssetConversion::swapExactTokensForTokensCall,
		env: &mut impl Ext<T = Runtime>,
	) -> Result<Vec<u8>, Error> {
		let path_len = Self::validated_path_len(&call.path)?;
		env.charge(
			<Runtime as pallet_asset_conversion::Config>::WeightInfo::swap_exact_tokens_for_tokens(
				path_len,
			),
		)?;
		let path: Vec<_> =
			call.path.iter().map(|e| Self::decode_asset_kind(e)).collect::<Result<_, _>>()?;

		let sender = Self::caller_account_id(env)?;
		let send_to = env.to_account_id(&H160(call.sendTo.0 .0));

		let amount_out = <pallet_asset_conversion::Pallet<Runtime> as Swap<
			<Runtime as frame_system::Config>::AccountId,
		>>::swap_exact_tokens_for_tokens(
			sender,
			path,
			Self::to_balance(call.amountIn)?,
			Some(Self::to_balance(call.amountOutMin)?),
			send_to,
			call.keepAlive,
		)?;

		Ok(IAssetConversion::swapExactTokensForTokensCall::abi_encode_returns(&Self::to_u256(
			amount_out,
		)?))
	}
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L519-545)
```rust
		/// Swap the exact amount of `asset1` into `asset2`.
		/// `amount_out_min` param allows you to specify the min amount of the `asset2`
		/// you're happy to receive.
		///
		/// [`AssetConversionApi::quote_price_exact_tokens_for_tokens`] runtime call can be called
		/// for a quote.
		#[pallet::call_index(3)]
		#[pallet::weight(T::WeightInfo::swap_exact_tokens_for_tokens(path.len() as u32))]
		pub fn swap_exact_tokens_for_tokens(
			origin: OriginFor<T>,
			path: Vec<Box<T::AssetKind>>,
			amount_in: T::Balance,
			amount_out_min: T::Balance,
			send_to: T::AccountId,
			keep_alive: bool,
		) -> DispatchResult {
			let sender = ensure_signed(origin)?;
			Self::do_swap_exact_tokens_for_tokens(
				sender,
				path.into_iter().map(|a| *a).collect(),
				amount_in,
				Some(amount_out_min),
				send_to,
				keep_alive,
			)?;
			Ok(())
		}
```

**File:** substrate/frame/revive/dev-node/runtime/src/lib.rs (L196-218)
```rust
/// Default extensions applied to Ethereum transactions.
#[derive(Clone, PartialEq, Eq, Debug)]
pub struct EthExtraImpl;

impl EthExtra for EthExtraImpl {
	type Config = Runtime;
	type ExtensionV0 = TxExtension;
	type ExtensionOtherVersions = sp_runtime::traits::InvalidVersion;

	fn get_eth_extension(nonce: u32, tip: Balance) -> Self::ExtensionV0 {
		(
			frame_system::CheckNonZeroSender::<Runtime>::new(),
			frame_system::CheckSpecVersion::<Runtime>::new(),
			frame_system::CheckTxVersion::<Runtime>::new(),
			frame_system::CheckGenesis::<Runtime>::new(),
			frame_system::CheckMortality::from(sp_runtime::generic::Era::Immortal),
			frame_system::CheckNonce::<Runtime>::from(nonce),
			frame_system::CheckWeight::<Runtime>::new(),
			pallet_transaction_payment::ChargeTransactionPayment::<Runtime>::from(tip),
			pallet_revive::evm::tx_extension::SetOrigin::<Runtime>::new_from_eth_transaction(),
			frame_system::WeightReclaim::<Runtime>::new(),
		)
	}
```

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/src/lib.rs (L1768-1799)
```rust
/// Default extensions applied to Ethereum transactions.
#[derive(Clone, PartialEq, Eq, Debug)]
pub struct EthExtraImpl;

impl EthExtra for EthExtraImpl {
	type Config = Runtime;
	type ExtensionV0 = TxExtension;
	type ExtensionOtherVersions = sp_runtime::traits::InvalidVersion;

	fn get_eth_extension(nonce: u32, tip: Balance) -> Self::ExtensionV0 {
		(
			frame_system::AuthorizeCall::<Runtime>::new(),
			frame_system::CheckNonZeroSender::<Runtime>::new(),
			frame_system::CheckSpecVersion::<Runtime>::new(),
			frame_system::CheckTxVersion::<Runtime>::new(),
			frame_system::CheckGenesis::<Runtime>::new(),
			frame_system::CheckMortality::from(generic::Era::Immortal),
			frame_system::CheckNonce::<Runtime>::from(nonce),
			frame_system::CheckWeight::<Runtime>::new(),
			pallet_pgas_allowance::ChargePGAS::<
				Runtime,
				pallet_asset_conversion_tx_payment::ChargeAssetTxPayment<Runtime>,
			>::new_skip_pgas(
				pallet_asset_conversion_tx_payment::ChargeAssetTxPayment::<Runtime>::from(
					tip, None,
				),
			),
			frame_metadata_hash_extension::CheckMetadataHash::<Runtime>::new(false),
			pallet_revive::evm::tx_extension::SetOrigin::<Runtime>::new_from_eth_transaction(),
		)
			.into()
	}
```
