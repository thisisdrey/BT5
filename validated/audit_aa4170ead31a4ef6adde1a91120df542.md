### Title
Stale oracle price reverts `DepositToken.withdraw`/`transfer`, temporarily freezing all user collateral - ([File: contracts/DepositToken.sol](contracts/DepositToken.sol))

### Summary
Metronome's `DepositToken` gates every balance-moving operation (`withdraw`, `transfer`, `transferFrom`, `seize`, `withdrawFrom`) behind `_revertIfLocked`, which calls `unlockedBalanceOf`, which unconditionally calls `Pool.debtPositionOf`. `debtPositionOf` prices *every* deposit-token underlying and every debt synthetic the account holds via `masterOracle.quoteTokenToUsd`. If the oracle reverts on a stale price (heartbeat/stale-period exceeded), all of these calls revert and the user's collateral is frozen until the feed is refreshed — even for accounts with zero debt. This is the same bug class as the JOJO finding: a staleness guard inside the pricing path liveness-blocks withdrawals.

### Finding Description
`withdraw` calls `_revertIfLocked(_msgSender, amount_)` before burning msdTOKEN and pulling collateral from the Treasury. `_revertIfLocked` consults `unlockedBalanceOf`, which calls `pool.debtPositionOf(account_)` before its own `debtInUsd == 0` early return [1](#0-0) .

`debtPositionOf` calls `debtOf`, which loops the account's debt tokens and calls `masterOracle.quoteTokenToUsd(syntheticToken, balance)` [2](#0-1) , and `depositOf`, which loops the account's deposit tokens and calls `masterOracle.quoteTokenToUsd(underlying, balance)` [3](#0-2) . Because `depositOf` runs for every collateral the user touches, a stale price on *any one* of them reverts the whole call — there is no `try/catch` or per-token isolation [4](#0-3) .

The oracle staleness behavior is real in the deployed configuration: fork/E2E tests show `pool.debtPositionOf` reverting with `price-expired` / `invalid-token-price` / `price-too-behind` when the feed is stale, and show a direct `msdUSDC.withdraw` reverting with `price-expired` until the pull oracle is updated in the same transaction via `Operator.execute` [5](#0-4) [6](#0-5) .

The same blocking applies to `transfer`/`transferFrom` (both call `_revertIfLocked`) [7](#0-6) , `withdraw` [8](#0-7) , and `withdrawFrom`/`seize`. Meanwhile `deposit` has no oracle dependency, so new funds can still flow in while exits are frozen [9](#0-8) .

### Impact Explanation
Temporary freezing of funds. While any upstream feed (Chainlink heartbeat or pull-oracle staleness window) for any asset in the user's deposit/debt set is expired, the user cannot withdraw collateral, transfer msdTOKEN, or be partially liquidated (`Pool.liquidate` also prices via `masterOracle`). For push-based feeds (e.g., Chainlink heartbeat 86400s on some chains), there is no permissionless refresh — the user must wait for the next feed update, matching the JOJO report exactly. For pull oracles the user can self-serve by prepending a `updatePrice`/`updatePriceFeeds` call through `Operator.execute`, which mitigates but does not eliminate the window (and doesn't exist for push feeds).

### Likelihood Explanation
Not attacker-triggered; it depends on oracle feed liveness. On chains where deployment config wires a feed with a heartbeat shorter than actual update cadence, or during feed outages / sequencer downtime, the lock occurs with certainty for every account holding that collateral. It is demonstrated reproducibly in the repo's own E2E tests. Severity is bounded to temporary freezing (the report's accepted class) rather than permanent loss.

### Recommendation
- In `unlockedBalanceOf`, wrap `debtPositionOf` (or each per-token oracle quote in `depositOf`/`debtOf`) in `try/catch`; on oracle failure for an account with no debt, fall back to returning `balanceOf[account_]`, or treat the stale token's USD value as 0 rather than reverting the entire call.
- Alternatively, give `withdraw`/`transfer` an escape path when `debtTokensOfAccount.length(account) == 0` that skips pricing entirely.
- For pull-oracle-backed assets, document/bundle the price update via `Operator.execute`; for push feeds, consider a fallback/emergency oracle as in the JOJO recommendation.

### Proof of Concept
Reproducible Hardhat-fork sketch (mirrors `test/Operator.test.ts:121` and `test/E2E.hemi.pullOracle.test.ts:108`):

```ts
// setup: alice deposits USDC into msdUSDC (no debt)
await usdc.approve(msdUSDC.address, amount);
await msdUSDC.deposit(amount, alice.address);

// advance time past the oracle stale period / heartbeat
await time.increase(time.duration.hours(2)); // > stalePeriod (e.g. 1h)

// every exit path reverts even though alice has zero debt
await expect(msdUSDC.withdraw(amount, alice.address)).revertedWith('price-expired');
await expect(msdUSDC.transfer(bob.address, 1)).revertedWith('price-expired');
await expect(pool.debtPositionOf(alice.address)).revertedWith('price-expired');

// only recovery for pull oracles: prepend updatePrice via Operator.execute
const calls = [
  { target: pullOracle.address, value: 0,
    callData: pullOracle.interface.encodeFunctionData('updatePrice', [usdc.address, price]) },
  { target: msdUSDC.address, value: 0,
    callData: msdUSDC.interface.encodeFunctionData('withdraw', [amount, alice.address]) },
];
await operator.connect(alice).execute(calls); // succeeds; for push feeds no such call exists
```

Caveat: the staleness check itself lives in the external `MasterOracle`/`DefaultOracle` (only `contracts/interfaces/external/IMasterOracle.sol` is in this repo), so the revert string comes from deployed oracle code, but the unconditional oracle dependency inside the withdraw/transfer path is in-scope Metronome code.

### Citations

**File:** contracts/DepositToken.sol (L211-237)
```text
    function deposit(
        uint256 amount_,
        address onBehalfOf_
    ) external override whenNotPaused nonReentrant onlyIfDepositTokenExists returns (uint256 _deposited, uint256 _fee) {
        if (amount_ == 0) revert AmountIsZero();
        if (onBehalfOf_ == address(0)) revert BeneficiaryIsNull();

        IPool _pool = pool;
        IERC20 _underlying = underlying;
        address _msgSender = _msgSender();
        address _treasury = address(_pool.treasury());

        if (_msgSender == _treasury) revert TreasuryCanNotDeposit();

        uint256 _balanceBefore = _underlying.balanceOf(_treasury);
        _underlying.safeTransferFrom(_msgSender, _treasury, amount_);
        amount_ = _underlying.balanceOf(_treasury) - _balanceBefore;

        (_deposited, _fee) = quoteDepositOut(amount_);
        if (_fee > 0) {
            _mint(_pool.feeCollector(), _fee);
        }

        _mint(onBehalfOf_, _deposited);

        emit CollateralDeposited(_msgSender, onBehalfOf_, amount_, _deposited, _fee);
    }
```

**File:** contracts/DepositToken.sol (L348-362)
```text
    function transfer(address to_, uint256 amount_) external override returns (bool) {
        address _msgSender = _msgSender();
        _revertIfLocked(_msgSender, amount_);

        _transfer(_msgSender, to_, amount_);
        return true;
    }

    /// @inheritdoc IERC20
    function transferFrom(
        address sender_,
        address recipient_,
        uint256 amount_
    ) external override nonReentrant returns (bool) {
        _revertIfLocked(sender_, amount_);
```

**File:** contracts/DepositToken.sol (L383-397)
```text
    function unlockedBalanceOf(address account_) public view override returns (uint256 _unlockedBalance) {
        IPool _pool = pool;

        (, , uint256 _debtInUsd, , uint256 _issuableInUsd) = _pool.debtPositionOf(account_);

        if (_debtInUsd == 0) {
            return balanceOf[account_];
        }

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

**File:** contracts/Pool.sol (L227-236)
```text
    function debtOf(address account_) public view override returns (uint256 _debtInUsd) {
        IMasterOracle _masterOracle = masterOracle();
        uint256 _length = debtTokensOfAccount.length(account_);
        for (uint256 i; i < _length; ++i) {
            IDebtToken _debtToken = IDebtToken(debtTokensOfAccount.at(account_, i));
            _debtInUsd += _masterOracle.quoteTokenToUsd(
                address(_debtToken.syntheticToken()),
                _debtToken.balanceOf(account_)
            );
        }
```

**File:** contracts/Pool.sol (L262-265)
```text
        _debtInUsd = debtOf(account_);
        (_depositInUsd, _issuableLimitInUsd) = depositOf(account_);
        _isHealthy = _debtInUsd <= _issuableLimitInUsd;
        _issuableInUsd = _debtInUsd < _issuableLimitInUsd ? _issuableLimitInUsd - _debtInUsd : 0;
```

**File:** contracts/Pool.sol (L274-288)
```text
    function depositOf(
        address account_
    ) public view override returns (uint256 _depositInUsd, uint256 _issuableLimitInUsd) {
        IMasterOracle _masterOracle = masterOracle();
        uint256 _length = depositTokensOfAccount.length(account_);
        for (uint256 i; i < _length; ++i) {
            IDepositToken _depositToken = IDepositToken(depositTokensOfAccount.at(account_, i));
            uint256 _amountInUsd = _masterOracle.quoteTokenToUsd(
                address(_depositToken.underlying()),
                _depositToken.balanceOf(account_)
            );
            _depositInUsd += _amountInUsd;
            _issuableLimitInUsd += _amountInUsd.wadMul(_depositToken.collateralFactor());
        }
    }
```

**File:** test/Operator.test.ts (L121-143)
```typescript
    it('should use operator to update oracle and write call', async function () {
      // given
      await time.increase(time.duration.hours(2))
      const withdrawAmount = parseUnits('50', 6)
      const tx = msdUSDC.connect(alice).withdraw(withdrawAmount, alice.address)
      await expect(tx).revertedWith('price-expired')

      expect(await msdUSDC.balanceOf(alice.address)).eq(parseUnits('100', 6))

      // when
      const updatePriceCallData = pullOracle.interface.encodeFunctionData('updatePrice', [
        usdc.address,
        parseEther('1'),
      ])
      const withdrawCallData = msdUSDC.interface.encodeFunctionData('withdraw', [withdrawAmount, alice.address])
      const calls: IOperator.CallStruct[] = [
        {target: pullOracle.address, value: 0, callData: updatePriceCallData},
        {target: msdUSDC.address, value: 0, callData: withdrawCallData},
      ]
      await operator.connect(alice).execute(calls)

      // then
      expect(await msdUSDC.balanceOf(alice.address)).eq(parseUnits('50', 6))
```

**File:** test/E2E.hemi.pullOracle.test.ts (L111-121)
```typescript
      await msdWETH.deposit(amount, alice.address)
      // if price is expired then call will fail
      const tx = pool.debtPositionOf(alice.address)
      await expect(tx).revertedWith('invalid-token-price')

      // when
      const wrappedPriceProvider = WrapperBuilder.wrap(pullPriceProvider).usingDataService({
        dataPackagesIds: ['ETH'],
      })
      // update price by directly calling priceProvider
      await wrappedPriceProvider.updatePrice([ETH_USD_FEED_ID])
```
