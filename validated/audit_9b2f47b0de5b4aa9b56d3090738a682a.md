### Title
Missing slippage protection on `VesperGateway.deposit` exposes users to share-price sandwich attacks - ([File: contracts/VesperGateway.sol](contracts/VesperGateway.sol))

### Summary
`VesperGateway.deposit` lets a user deposit a Vesper pool's underlying asset and routes the minted vTokens into `DepositToken.deposit` on their behalf. The call to `vToken_.deposit(amount_)` at `contracts/VesperGateway.sol:57` accepts no minimum-shares parameter, so the user receives however many vTokens the pool's current price-per-share yields. An unprivileged attacker can manipulate the Vesper pool's share price (e.g., by donating underlying directly to the pool to inflate `totalValue`/price-per-share) in the same block, causing the victim to mint far fewer vTokens — and therefore far fewer `msdToken` collateral tokens — than expected, then redeem their own shares to profit.

### Finding Description
The flow at `contracts/VesperGateway.sol:44-65`:

1. `pool_` must be a registered pool (`isPoolRegistered` check at line 45), but `vToken_` is attacker/user-chosen and only constrained by `pool_.depositTokenOf(vToken_)` returning a valid `DepositToken` — which is the normal configuration for Vesper-backed collateral like `vaUSDC`/`vaETH`.
2. `_underlying.safeTransferFrom` pulls the user's tokens (line 51).
3. `vToken_.deposit(amount_)` mints shares at the *current* share price with no `minOut`/slippage bound (lines 56–58). The minted amount is taken via balance delta.
4. The resulting vTokens are deposited via `_depositToken.deposit(_vTokenAmount, _msgSender)` (line 64), minting `msdToken` 1:1 (minus fee) per `DepositToken.deposit` (`contracts/DepositToken.sol:211-237`), which itself mints shares 1:1 rather than via a manipulable exchange rate — so all slippage risk lives in the upstream Vesper mint.

Vesper `VPool` share pricing is derived from `totalValue()` which includes the pool's token balance; a direct donation to the pool before the victim's transaction inflates price-per-share, so the victim's `amount_` buys fewer shares. The attacker, holding pre-minted shares, redeems after the victim's deposit and captures the difference. `deposit` has no `nonReentrant` guard and no slippage parameter at all, so nothing in Metronome bounds the outcome; `SynthContext._msgSender` only determines the recipient, not the price.

### Impact Explanation
Direct theft of user funds: the victim's deposited underlying is partially captured by the attacker via the manipulated share price, and the victim receives proportionally fewer collateral tokens in Metronome. The loss is bounded only by how far the attacker can move the pool's share price.

### Likelihood Explanation
Requires a Vesper pool whose share price is manipulable via balance donations/front-running and that is registered as collateral (`depositTokenOf` returns a valid token). Vesper `VPool` shares are exactly the yield-vault mint pattern in the original report (the zap deposited into a Badger/ibBTC vault minting shares at a manipulable rate). The gateway is a deployed production entry point callable by anyone. Likelihood depends on mempool visibility of `deposit` calls, which are plain public transactions.

### Recommendation
Add a `minVTokenAmount_` (or equivalent `minOut`) parameter to `VesperGateway.deposit` and revert if `_vTokenAmount < minVTokenAmount_` after the balance-delta measurement at line 58, mirroring the existing slippage pattern used in `SmartFarmingManager.flashRepay`/`leverage` (`swapAmountOutMin_`, `depositAmountMin_` at `contracts/SmartFarmingManager.sol:96-103,153-162`).

### Proof of Concept
Conceptual fork test (Hardhat/Foundry against mainnet Vesper pools, e.g. `vaUSDC` registered in the deployed `Pool`):

```solidity
// Setup: fork mainnet; use deployed PoolRegistry/Pool/VesperGateway and real vUSDC pool
// 1. Attacker acquires vToken shares normally (vToken.deposit(x)).
// 2. Victim submits gateway.deposit(pool, vToken, amount).
// 3. Attacker front-runs: underlying.transfer(address(vTokenPool), bigDonation)
//    -> pool totalValue() inflated -> pricePerShare rises.
// 4. Victim tx executes: vToken_.deposit(amount_) mints fewer shares;
//    assert vTokenAmount < expectedShares (no revert possible today).
// 5. Attacker redeems all shares via vToken.withdraw / pool withdraw,
//    receiving more underlying than deposited -> profit = victim's loss.
```

Note: the exact exploitability depends on whether the specific deployed Vesper pool version includes donation-inflation resistance in `totalValue()` (some VPool versions use reported strategy balances). If a target pool's share price cannot be moved in the same block, the analog does not hold for that collateral; verification requires a mainnet fork test against the actual registered `vToken` deployments.