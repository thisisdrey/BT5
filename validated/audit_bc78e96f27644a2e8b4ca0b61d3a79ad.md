### Title
Liquidations can proceed while users are unable to deposit or withdraw collateral when the underlying token is paused, because `seize()` only updates internal msdToken balances and never transfers the underlying - (File: contracts/Pool.sol)

### Summary
`Pool.liquidate` settles liquidations entirely through `DepositToken.seize()`, which performs an internal `_transfer` of deposit-token (msdToken) balances and never moves the underlying collateral token. In contrast, `DepositToken.deposit` calls `underlying.safeTransferFrom` into the Treasury and `DepositToken._withdraw` calls `treasury.pull`, which pushes the underlying out. If the underlying collateral token (e.g. USDC) is paused at the token contract level, every user action that could prevent liquidation (depositing more collateral, withdrawing to unwind, flash-repay flows that pull underlying) reverts, while `Pool.liquidate` still executes fully because it only rewrites share balances. This is the same bug class as GMX M-11: liquidation requires no actual token transfer, while the user's defensive actions do.

### Finding Description
- `DepositToken.deposit` pulls `underlying` via `safeTransferFrom(msgSender, treasury, amount_)` (contracts/DepositToken.sol:225-227). If the underlying is paused, this reverts — a user cannot add collateral to restore health.
- `DepositToken._withdraw` ends with `_pool.treasury().pull(to_, _withdrawn)` (contracts/DepositToken.sol:551), which transfers underlying out of the Treasury — also reverts on a paused token.
- `Pool.liquidate` only calls `syntheticToken_.burn`, `debtToken.burn`, and `depositToken_.seize(account_, _msgSender, _toLiquidator)` plus a second `seize` to the feeCollector (contracts/Pool.sol:587-593).
- `DepositToken.seize` is `onlyIfCanSeize` (only callable by the Pool) and forwards to `_transfer` (contracts/DepositToken.sol:343-345), which only mutates `balanceOf` mappings (contracts/DepositToken.sol:498-526). No underlying ERC20 transfer occurs anywhere in the liquidation path.
- The only guards on `liquidate` are `whenNotShutdown`, `nonReentrant`, and token-existence checks (contracts/Pool.sol:543-549) — none of these react to an underlying token pause.

A user's only alternative defense is repaying debt with synthetic tokens they already hold (`DebtToken.repay`/`Pool.swap` are also pure internal burn/mint), but a user without sufficient msToken balance cannot acquire more without depositing collateral or trading externally — both blocked or unavailable during the pause.

### Impact Explanation
Direct loss of user funds / liquidation that the victim is structurally unable to prevent: an underwater (or liquidatable-threshold) position is seized even though the protocol offers no working path for the user to cure it, because every curative action that touches the underlying reverts while the liquidation path does not. The user loses `_toLiquidator` collateral to the liquidator and `_fee` to the feeCollector (contracts/Pool.sol:589-593).

### Likelihood Explanation
Low-to-medium. It requires the underlying collateral token to be paused (e.g. USDC/USDT `pause()`/`blocklist` events, which have occurred historically) or a Metronome `DepositToken` flag that blocks underlying movement, while `Pool.everythingStopped` remains `false` (docs/emergency-flags.md confirms `liquidate` is disabled only by `everythingStopped`, and `deposit` only by `paused`/`isActive`). During that window any liquidatable position can be taken permissionlessly by any account holding the synthetic debt token.

### Recommendation
Have `Pool.liquidate` force an actual underlying transfer — e.g. `seize` could additionally pull underlying from the Treasury to the liquidator, or burn the victim's msdTokens and `treasury.pull` the underlying to the liquidator — so a paused collateral token blocks liquidation symmetrically. Alternatively/additionally, add a permissionless or guardian-triggered check that pauses `liquidate` for a market whose underlying transfers are verified to fail.

### Proof of Concept
Hardhat-style sketch against the repo's existing test fixtures (mirroring `test/Pool.test.ts` liquidation setup):

```ts
// Given: alice deposited MET via msdMET and issued msETH debt; price drop makes her position unhealthy.
const {_isHealthy} = await pool.debtPositionOf(alice.address)
expect(_isHealthy).false

// When: underlying collateral token is paused (use a pausable ERC20 mock as `met`)
await met.pause()

// Then: user cannot cure
await expect(msdMET.connect(alice).deposit(amount, alice.address)).reverted      // safeTransferFrom reverts
await expect(msdMET.connect(alice).withdraw(someAmount, alice.address)).reverted // treasury.pull reverts

// But: liquidator can still seize her collateral
const amountToRepay = await pool.quoteLiquidateMax(msEth.address, alice.address, msdMET.address)
await pool.connect(liquidator).liquidate(msEth.address, alice.address, amountToRepay, msdMET.address)
// succeeds — alice's msdMET balance drops, liquidator's increases; no underlying moved
``` [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** contracts/Pool.sol (L587-593)
```text
        syntheticToken_.burn(_msgSender, amountToRepay_);
        _debtToken.burn(account_, amountToRepay_);
        depositToken_.seize(account_, _msgSender, _toLiquidator);

        if (_fee > 0) {
            depositToken_.seize(account_, _poolRegistry.feeCollector(), _fee);
        }
```

**File:** contracts/DepositToken.sol (L225-227)
```text
        uint256 _balanceBefore = _underlying.balanceOf(_treasury);
        _underlying.safeTransferFrom(_msgSender, _treasury, amount_);
        amount_ = _underlying.balanceOf(_treasury) - _balanceBefore;
```

**File:** contracts/DepositToken.sol (L343-345)
```text
    function seize(address from_, address to_, uint256 amount_) external override onlyIfCanSeize {
        _transfer(from_, to_, amount_);
    }
```

**File:** contracts/DepositToken.sol (L550-553)
```text
        _burn(account_, _withdrawn);
        _pool.treasury().pull(to_, _withdrawn);

        emit CollateralWithdrawn(account_, to_, amount_, _withdrawn, _fee);
```

**File:** docs/emergency-flags.md (L63-85)
```markdown
## Pool.liquidate()

Disabled if: `PoolRegistry.everythingStopped()` || `Pool.everythingStopped()`

## DebtToken.issue()

Disabled if: `PoolRegistry.everythingStopped()` || `!SyntheticToken.isActive()` || `Pool.everythingStopped()` || `!DebtToken.isActive()`

## DebtToken.repay()

Disabled if: `PoolRegistry.everythingStopped()` || `Pool.everythingStopped()`

## DebtToken.repayAll()

Disabled if: `PoolRegistry.everythingStopped()` || `Pool.everythingStopped()`

## DepositToken.deposit()

Disabled if: `PoolRegistry.paused()` || `Pool.paused()` || `!DepositToken.isActive()`

## DepositToken.withdraw()

Disabled if: `PoolRegistry.everythingStopped()` || `Pool.everythingStopped()`
```
