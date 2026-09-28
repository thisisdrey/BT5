### Title
Paused or frozen underlying collateral token permanently locks all deposits — `DepositToken._withdraw` has no alternative exit path — ([File: contracts/DepositToken.sol](contracts/DepositToken.sol))

### Summary
`DepositToken` is the sole redemption path for a collateral position: burning `msdTOKEN` via `_withdraw` always ends in `Treasury.pull`, which does an unconditional `underlying.safeTransfer(to_, amount_)`. If the underlying third-party token becomes paused or the `Treasury`/recipient is frozen (e.g., a pausable/blacklistable asset such as USDC/USDT, or a Vesper vToken whose transfer path reverts), every withdrawal, `flashWithdraw`, `withdrawFrom`, and SmartFarmingManager unwind reverts. `underlying` is immutable and there is no mechanism to route around the token, migrate collateral, or withdraw in an alternative asset, so all user collateral in that `DepositToken` is locked for as long as the third party remains paused — permanently if the pause is permanent. This mirrors the reported bug class: one paused third-party dependency blocks all withdrawals with no exclusion mechanism.

### Finding Description
The full withdrawal flow is:

- `DepositToken.withdraw(amount_, to_)` → `_revertIfLocked` → `_withdraw` [1](#0-0) 
- `DepositToken.withdrawFrom` and `flashWithdraw` (SmartFarmingManager paths) also funnel into `_withdraw` [2](#0-1) [3](#0-2) 
- `_withdraw` burns the deposit token and unconditionally calls `_pool.treasury().pull(to_, _withdrawn)` [4](#0-3) 
- `Treasury.pull` performs `underlying().safeTransfer(to_, amount_)` — a direct transfer of the third-party token [5](#0-4) 
- `underlying` is set once in `initialize` and has no setter or migration path [6](#0-5) 

Unlike deposits — which can be halted granularly per collateral via `DepositToken.isActive` and `Pool.paused` while `withdraw` keeps working (withdraw is only gated by `whenNotShutdown`) [7](#0-6) [8](#0-7)  — there is no equivalent way to keep exits working when the *underlying token itself* is the paused component. Transferring the `msdTOKEN` share (`_transfer`) or seizing it in liquidation only moves the claim; redemption of the underlying always goes through the same reverting `safeTransfer`.

The same single-point-of-failure exists one level up: `unlockedBalanceOf` calls `pool.masterOracle().quoteUsdToToken(underlying, ...)`, so an underlying whose oracle feed reverts also bricks `withdraw`/`transfer` via `_revertIfLocked` [9](#0-8) .

### Impact Explanation
If the underlying token pauses transfers (or the Treasury gets blacklisted/frozen for a blacklistable stablecoin collateral), every holder of that `msdTOKEN` loses the ability to redeem collateral. `msdTOKEN` transfers still work but are worthless without redemption; `seize` in liquidation likewise cannot extract underlying. If the third-party pause is permanent, all collateral for that market is permanently locked in `Treasury` — identical in consequence to the reported "permanently locked vault funds," broken liveness invariant.

### Likelihood Explanation
Metronome explicitly supports arbitrary ERC20 collateral including Vesper vTokens via `VesperGateway`, and major supported collateral types (USDC, USDT, and other pausable/blacklistable tokens) are exactly the assets that implement global pause/blacklist. No privileged action, governance misconfiguration, or attacker setup is required — the trigger is an external third-party event, same as the Aave V3 pool pause in the original report. No deployed-configuration guard prevents it: `whenNotShutdown` only checks the pool flag, not the underlying's state [10](#0-9) .

### Recommendation
Add an emergency escape that does not depend on the underlying token's liveness, e.g.:

- A governor-enabled `emergencyWithdraw`/`rescueMode` flag per `DepositToken` that lets `msdTOKEN` holders burn shares into a claim recorded on-chain, redeemable later if/when the token unpauses, or redeemable in an alternate asset.
- Or allow `Treasury.pull` to support a per-token fallback recipient/rerouting mechanism so a frozen address doesn't brick everyone (this only covers the blacklist case, not a global token pause — the claim-ledger approach covers both).
- At minimum, make `unlockedBalanceOf` tolerant of oracle failure for the zero-debt case so a reverted feed doesn't block exits for users with no debt.

### Proof of Concept
Hardhat fork (mainnet), using a pausable collateral e.g. USDC as `underlying`:

```ts
// Setup: deploy Pool, Treasury, DepositToken with underlying = USDC
await usdc.connect(whale).approve(depositToken.address, amount);
await depositToken.connect(whale).deposit(amount, alice.address); // alice holds msdUSDC

// Third-party event: Circle pauses USDC (or blacklists Treasury)
await usdc.connect(pauser).pause(); // mainnet USDC has pause()/blacklist()

// Now every exit path reverts inside Treasury.pull -> safeTransfer
await expect(
  depositToken.connect(alice).withdraw(aliceBal, alice.address)
).to.be.reverted; // USDC paused -> transfer fails -> IsShutdown is NOT the cause; token revert

await expect(
  depositToken.connect(sfm).flashWithdraw(alice.address, 1)
).to.be.reverted;

// No alternative: underlying is immutable, no claim mechanism, seize() only moves shares
```

The invariant broken is liveness/redemption: `msdTOKEN` totalSupply is fully backed by underlying in `Treasury`, but no user-facing path can move that underlying while the third-party token is paused, and the protocol exposes no exclusion or delayed-claim mechanism to route around it.

### Citations

**File:** contracts/DepositToken.sol (L158-175)
```text
    ) external initializer {
        if (address(underlying_) == address(0)) revert UnderlyingAssetIsNull();
        if (address(pool_) == address(0)) revert PoolIsNull();
        if (bytes(symbol_).length == 0) revert SymbolIsNull();
        if (decimals_ == 0) revert DecimalsIsNull();
        if (collateralFactor_ == 0) revert CollateralFactorTooLow();
        if (collateralFactor_ >= 1e18) revert CollateralFactorTooHigh();

        __Manageable_init(pool_);

        name = name_;
        symbol = symbol_;
        underlying = underlying_;
        isActive = true;
        decimals = decimals_;
        collateralFactor = collateralFactor_;
        maxTotalSupply = maxTotalSupply_;
    }
```

**File:** contracts/DepositToken.sol (L245-250)
```text
    function flashWithdraw(
        address account_,
        uint256 amount_
    ) external override onlyIfSmartFarmingManager returns (uint256 _withdrawn, uint256 _fee) {
        return _withdraw({account_: account_, amount_: amount_, to_: _msgSender()});
    }
```

**File:** contracts/DepositToken.sol (L392-397)
```text
        if (_issuableInUsd > 0) {
            _unlockedBalance = Math.min(
                balanceOf[account_],
                _pool.masterOracle().quoteUsdToToken(address(underlying), _issuableInUsd.wadDiv(collateralFactor))
            );
        }
```

**File:** contracts/DepositToken.sol (L406-412)
```text
    function withdraw(uint256 amount_, address to_) external override returns (uint256 _withdrawn, uint256 _fee) {
        if (to_ == address(0)) revert RecipientIsNull();
        address _msgSender = _msgSender();
        _revertIfLocked(_msgSender, amount_);

        return _withdraw({account_: _msgSender, amount_: amount_, to_: to_});
    }
```

**File:** contracts/DepositToken.sol (L420-427)
```text
    function withdrawFrom(
        address from_,
        uint256 amount_
    ) external override onlyIfSmartFarmingManager returns (uint256 _withdrawn, uint256 _fee) {
        _revertIfLocked(from_, amount_);

        return _withdraw({account_: from_, amount_: amount_, to_: _msgSender()});
    }
```

**File:** contracts/DepositToken.sol (L536-554)
```text
    function _withdraw(
        address account_,
        uint256 amount_,
        address to_
    ) private whenNotShutdown nonReentrant onlyIfDepositTokenExists returns (uint256 _withdrawn, uint256 _fee) {
        if (amount_ == 0) revert AmountIsZero();

        IPool _pool = pool;

        (_withdrawn, _fee) = quoteWithdrawOut(amount_);
        if (_fee > 0) {
            _transfer(account_, _pool.feeCollector(), _fee);
        }

        _burn(account_, _withdrawn);
        _pool.treasury().pull(to_, _withdrawn);

        emit CollateralWithdrawn(account_, to_, amount_, _withdrawn, _fee);
    }
```

**File:** contracts/Treasury.sol (L66-72)
```text
    function pull(address to_, uint256 amount_) external override nonReentrant {
        address _msgSender = _msgSender();
        if (!pool.doesDepositTokenExist(IDepositToken(_msgSender))) revert SenderIsNotDepositToken();
        if (to_ == address(0)) revert RecipientIsNull();
        if (amount_ == 0) revert AmountIsZero();
        IDepositToken(_msgSender).underlying().safeTransfer(to_, amount_);
    }
```

**File:** docs/emergency-flags.md (L79-85)
```markdown
## DepositToken.deposit()

Disabled if: `PoolRegistry.paused()` || `Pool.paused()` || `!DepositToken.isActive()`

## DepositToken.withdraw()

Disabled if: `PoolRegistry.everythingStopped()` || `Pool.everythingStopped()`
```

**File:** contracts/utils/Pauseable.sol (L50-54)
```text
    modifier whenNotShutdown() {
        if (everythingStopped()) revert IsShutdown();
        _;
    }

```
