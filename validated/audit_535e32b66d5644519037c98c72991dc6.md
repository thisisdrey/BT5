### Title
Unprivileged dust-transfer griefing of `feeCollector` permanently reverts `Pool.liquidate` whenever `protocolFee > 0`, freezing protocol-wide liquidations - ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
`Pool.liquidate` seizes a protocol-fee share of collateral to `poolRegistry.feeCollector()` via `DepositToken.seize`, which internally calls `_transfer` → `pool.addToDepositTokensOfAccount(feeCollector)` when the collector's balance of that deposit token is zero. That add is guarded by `onlyIfAdditionWillNotReachMaxTokens`, reverting with `UserReachedMaxTokens` once the account holds 30 distinct deposit/debt tokens (`MAX_TOKENS_PER_USER`). An unprivileged attacker can push `feeCollector` to that cap by depositing dust and transferring 1 wei of each deposit token to it, causing every `liquidate` call that charges a fee to revert — a repeatable crash of the liquidation path analogous to CVE-2020-2930's complete-DoS bug class.

### Finding Description
The revert chain:

1. `Pool.liquidate` → `depositToken_.seize(account_, _poolRegistry.feeCollector(), _fee)` when `_fee > 0` [1](#0-0) 
2. `DepositToken.seize` → `_transfer(from, feeCollector, amount)`, which calls `pool.addToDepositTokensOfAccount(feeCollector)` if the collector previously had a zero balance [2](#0-1) 
3. `Pool.addToDepositTokensOfAccount` is wrapped in `onlyIfAdditionWillNotReachMaxTokens`, which reverts when `debtTokensOfAccount.length + depositTokensOfAccount.length >= 30` [3](#0-2) [4](#0-3) 

Any EOA can inflate another account's per-account token list by sending dust: `DepositToken.transfer` only checks the *sender's* unlocked balance (`_revertIfLocked(_msgSender, amount_)`), never the recipient's [5](#0-4) . The attacker deposits dust into every registered `DepositToken` and transfers 1 wei of each to `feeCollector`, filling all 30 slots. Because `liquidate` is `nonReentrant` and atomic, the revert in step 3 rolls back the entire liquidation, including the debt burn and the liquidator's seize [6](#0-5) .

The same revert also hits `DepositToken.withdraw`/`_withdraw`, which transfers the withdraw fee to `feeCollector` [7](#0-6) , and `DebtToken.issue`/`repay` fee mints are unaffected only because they mint the *synthetic* token, not a deposit token.

### Impact Explanation
- Broken invariant: liquidation liveness. While `feeCollector` sits at the token cap and `protocolFee > 0`, **every** `liquidate` call on every account and every collateral reverts with `UserReachedMaxTokens`. Underwater positions cannot be closed, so bad debt accrues interest and grows — protocol insolvency risk plus temporary freezing of all seizable collateral.
- Withdrawals that incur a fee are likewise bricked for any collateral token the collector doesn't already hold, temporarily freezing user funds behind fee-paying exits.
- The state is reversible (the collector can transfer dust out, removing each token on balance→0 [8](#0-7) ), matching the "temporary freezing of funds" / repeatable-DoS impact class of the source CVE rather than permanent loss.

### Likelihood Explanation
- Requirements: (a) `feeProvider.liquidationFees().protocolFee > 0` (checked per-liquidation at `quoteLiquidateOut` [9](#0-8) ); (b) the pool must have enough registered `DepositToken`s for the attacker to reach 30 entries — since `issue` mints debt to the caller, not to an arbitrary `to_` [10](#0-9) , debt-token entries can only be pushed to `feeCollector` via `SmartFarmingManager` mint paths, so realistically the attacker needs ~30 deposit tokens listed or a partially filled collector list. This is the main constraint and depends on the deployed configuration; pools with few collaterals are not exploitable.
- Cost is only dust deposits plus gas; no privileged role, oracle manipulation, or trusted-remote assumption is needed. `whenNotShutdown`, `nonReentrant`, `SynthContext`, and `onlyIf*Exists` modifiers do not stop it — the grief vector is ordinary `transfer`.
- Uncertainty: if `feeCollector` is a contract that actively sweeps tokens, it can self-heal; if it's an EOA/multisig, cleanup requires manual transactions per token.

### Recommendation
- In `DepositToken._transfer`, skip `pool.addToDepositTokensOfAccount` (or catch its revert) when the recipient is `pool.feeCollector()` — feeCollector balances do not back debt and don't need tracked positions.
- Alternatively, bound-check only additions triggered by user-facing deposits/mints, and make seize/fee paths tolerant (e.g., wrap the add in a try/catch in `Pool.liquidate`).
- Long-term, consider removing the `MAX_TOKENS_PER_USER` revert in favor of iterating views off-chain, or auto-evicting zero/dust entries.

### Proof of Concept
Hardhat (TypeScript) fork-style sketch against deployed pool wiring:

```ts
// contracts/test: feeCollector liquidation DoS
it('griefs feeCollector so all liquidations revert with UserReachedMaxTokens', async () => {
  const pool: Pool = /* deployed Pool */;
  const feeCollector = await poolRegistry.feeCollector();
  const depositTokens: string[] = await pool.getDepositTokens();

  // 1) Attacker deposits dust into each deposit token and sends 1 wei to feeCollector
  for (const addr of depositTokens.slice(0, 30)) {
    const dt = await ethers.getContractAt('DepositToken', addr);
    const underlying = await ethers.getContractAt('ERC20Mock', await dt.underlying());
    await underlying.mint(attacker.address, 10);
    await underlying.connect(attacker).approve(dt.address, 10);
    await dt.connect(attacker).deposit(10, attacker.address); // attacker gets msdXXX
    await dt.connect(attacker).transfer(feeCollector, 1);     // adds token to collector's list
  }
  const held =
    (await pool.getDepositTokensOfAccount(feeCollector)).length +
    (await pool.getDebtTokensOfAccount(feeCollector)).length;
  expect(held).to.eq(await pool.MAX_TOKENS_PER_USER());

  // 2) Any unhealthy position now cannot be liquidated while protocolFee > 0
  await masterOracle.updatePrice(collateral.address, crashedPrice); // make alice unhealthy
  const amountToRepay = await pool.quoteLiquidateMax(msEth.address, alice.address, msdToken.address);
  await expect(
    pool.connect(liquidator).liquidate(msEth.address, alice.address, amountToRepay, msdToken.address)
  ).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');

  // 3) Sanity: with protocolFee == 0 the same call succeeds (isolates the fee-seize path)
  await feeProvider.updateProtocolLiquidationFee(0);
  await pool.connect(liquidator).liquidate(msEth.address, alice.address, amountToRepay, msdToken.address);
});
```

Note: the PoC requires a pool configuration with ≥30 registered deposit tokens (or a partially pre-filled `feeCollector` list); on smaller deployments the attack surface does not exist and the finding reduces to a latent design risk.

### Citations

**File:** contracts/Pool.sol (L143-148)
```text
    modifier onlyIfAdditionWillNotReachMaxTokens(address account_) {
        if (debtTokensOfAccount.length(account_) + depositTokensOfAccount.length(account_) >= MAX_TOKENS_PER_USER) {
            revert UserReachedMaxTokens();
        }
        _;
    }
```

**File:** contracts/Pool.sol (L216-220)
```text
    function addToDepositTokensOfAccount(address account_) external onlyIfAdditionWillNotReachMaxTokens(account_) {
        address _depositToken = _msgSender();
        _revertIfSenderIsNotDepositToken(_depositToken);
        if (!depositTokensOfAccount.add(account_, _depositToken)) revert DepositTokenAlreadyExists();
    }
```

**File:** contracts/Pool.sol (L462-466)
```text
        (uint128 _liquidatorIncentive, uint128 _protocolFee) = feeProvider.liquidationFees();

        if (_protocolFee > 0) {
            _fee = _toLiquidator.wadMul(_protocolFee);
        }
```

**File:** contracts/Pool.sol (L537-596)
```text
    function liquidate(
        ISyntheticToken syntheticToken_,
        address account_,
        uint256 amountToRepay_,
        IDepositToken depositToken_
    )
        external
        override
        whenNotShutdown
        nonReentrant
        onlyIfSyntheticTokenExists(syntheticToken_)
        onlyIfDepositTokenExists(depositToken_)
        returns (uint256 _totalSeized, uint256 _toLiquidator, uint256 _fee)
    {
        address _msgSender = _msgSender();

        if (amountToRepay_ == 0) revert AmountIsZero();
        if (_msgSender == account_) revert CanNotLiquidateOwnPosition();

        IDebtToken _debtToken = debtTokenOf[syntheticToken_];
        _debtToken.accrueInterest();

        (bool _isHealthy, , , , ) = debtPositionOf(account_);

        if (_isHealthy) {
            revert PositionIsHealthy();
        }

        uint256 _debtTokenBalance = _debtToken.balanceOf(account_);

        if (amountToRepay_.wadDiv(_debtTokenBalance) > maxLiquidable) {
            revert AmountGreaterThanMaxLiquidable();
        }

        if (debtFloorInUsd > 0) {
            uint256 _newDebtInUsd = masterOracle().quoteTokenToUsd(
                address(syntheticToken_),
                _debtTokenBalance - amountToRepay_
            );
            if (_newDebtInUsd > 0 && _newDebtInUsd < debtFloorInUsd) {
                revert RemainingDebtIsLowerThanTheFloor();
            }
        }

        (_totalSeized, _toLiquidator, _fee) = quoteLiquidateOut(syntheticToken_, amountToRepay_, depositToken_);

        if (_totalSeized > depositToken_.balanceOf(account_)) {
            revert AmountIsTooHigh();
        }

        syntheticToken_.burn(_msgSender, amountToRepay_);
        _debtToken.burn(account_, amountToRepay_);
        depositToken_.seize(account_, _msgSender, _toLiquidator);

        if (_fee > 0) {
            depositToken_.seize(account_, _poolRegistry.feeCollector(), _fee);
        }

        emit PositionLiquidated(_msgSender, account_, syntheticToken_, amountToRepay_, _totalSeized, _fee);
    }
```

**File:** contracts/DepositToken.sol (L348-354)
```text
    function transfer(address to_, uint256 amount_) external override returns (bool) {
        address _msgSender = _msgSender();
        _revertIfLocked(_msgSender, amount_);

        _transfer(_msgSender, to_, amount_);
        return true;
    }
```

**File:** contracts/DepositToken.sol (L517-520)
```text
        // Add this token to the deposit tokens list if the recipient is receiving it for the 1st time
        if (_recipientBalanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(recipient_);
        }
```

**File:** contracts/DepositToken.sol (L522-525)
```text
        // Remove this token from the deposit tokens list if the sender's balance goes to zero
        if (amount_ > 0 && balanceOf[sender_] == 0) {
            pool.removeFromDepositTokensOfAccount(sender_);
        }
```

**File:** contracts/DepositToken.sol (L545-551)
```text
        (_withdrawn, _fee) = quoteWithdrawOut(amount_);
        if (_fee > 0) {
            _transfer(account_, _pool.feeCollector(), _fee);
        }

        _burn(account_, _withdrawn);
        _pool.treasury().pull(to_, _withdrawn);
```

**File:** contracts/DebtToken.sol (L250-262)
```text
        address _msgSender = _msgSender();
        IPool _pool = pool;
        ISyntheticToken _syntheticToken = syntheticToken;

        (, , , , uint256 _issuableInUsd) = _pool.debtPositionOf(_msgSender);

        IMasterOracle _masterOracle = _pool.masterOracle();

        if (amount_ > _masterOracle.quoteUsdToToken(address(_syntheticToken), _issuableInUsd)) {
            revert NotEnoughCollateral();
        }

        _mint(_pool, _masterOracle, _msgSender, amount_);
```
