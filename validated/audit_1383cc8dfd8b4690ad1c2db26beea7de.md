### Title
Oracle update front-running allows borrowers to retire depreciated synthetic debt and keep unbacked surplus - ([File: contracts/Pool.sol])

### Summary
`Pool.swap` prices synthetic-token exchanges using the current `MasterOracle` quote and atomically burns the input token while minting the output token. An attacker can issue a synthetic asset, swap it immediately before an oracle update lowers that asset’s price, repurchase the same synthetic amount at the lower post-update price, repay the debt, withdraw all collateral, and retain the difference in the other synthetic asset. This leaves synthetic supply outstanding without the corresponding borrower debt and lets the attacker repeatedly extract value around oracle updates. [1](#0-0) [2](#0-1) 

### Finding Description
`DebtToken.issue` permits a caller to mint synthetic debt up to the account’s current `_issuableInUsd`, which is calculated from current oracle prices and collateral factors. [3](#0-2)  The synthetic debt remains denominated in units of the issued synthetic asset, while `debtOf` converts that fixed unit balance to USD using the latest oracle price. [4](#0-3) 

`Pool.swap` does not transfer debt or perform a collateral check; it only verifies that both tokens are registered, the swap feature is active, and the caller owns the input synthetic balance. [5](#0-4)  It then burns `syntheticTokenIn_`, obtains an immediate oracle conversion through `quoteSwapOut`, and mints `syntheticTokenOut_` to the caller. [6](#0-5)  The quote is a direct `MasterOracle.quote` conversion between the two synthetic assets, reduced only by any configured swap fee. [7](#0-6) 

After the price moves, `DebtToken.repay` burns the received synthetic token and reduces the borrower’s fixed-unit debt by the repaid amount. [8](#0-7)  Once debt reaches zero, `DepositToken.unlockedBalanceOf` returns the full deposit-token balance, and `withdraw`/`_withdraw` returns the underlying collateral from `Treasury`. [9](#0-8) [10](#0-9) 

The exploited invariant is the presumed USD equivalence between synthetic-token supply and borrower debt. `swap` conserves the quoted USD value only at the instant it executes; it does not reprice the outstanding debt token or reserve the future value needed to unwind that debt. A pending oracle update therefore creates a deterministic window in which old prices can be used for the first leg and new prices for the second leg. [11](#0-10) [12](#0-11) 

### Impact Explanation
The attacker can withdraw the original collateral while retaining synthetic tokens acquired by trading around the oracle transition. The retained tokens are not backed by the attacker’s debt after repayment, so continued repetitions increase protocol-wide synthetic liabilities without corresponding debt and can eventually produce insolvency or losses for other synthetic holders and liquidators. [13](#0-12) [14](#0-13) 

For example, with 1 WETH collateral quoted at `$2,000`, a `75%` collateral factor, and zero fees:

1. Deposit 1 WETH and issue `0.75 msETH`, representing `$1,500` of debt.
2. Before an oracle update from `$2,000` to `$1,800`, call `swap(msETH, msUSD, 0.75e18)`.
3. The pool burns `0.75 msETH` and mints `1,500 msUSD` using the stale price.
4. After the update, swap `1,350 msUSD` to `0.75 msETH`.
5. Call `repayAll` and withdraw all deposited WETH.
6. Keep the remaining `150 msUSD`, less fees and execution costs.

The profit scales linearly with borrowed size and the magnitude of the price update. [15](#0-14) [2](#0-1) 

### Likelihood Explanation
The attack requires only an unprivileged account, collateral sufficient to issue debt, and knowledge of a pending favorable oracle update. Pull-oracle flows expose this timing particularly clearly because the public update transaction can be observed and front-run, and `Operator.execute` can bundle user calls without changing authorization semantics. [16](#0-15) [17](#0-16) 

No privileged role is needed for `issue`, `swap`, `repay`, or `withdraw`. `swap` is initialized as active, and the tested path permits synthetic-to-synthetic exchanges. [18](#0-17)  Reentrancy protection, token registration checks, supply caps, and the final position checks do not prevent the attack because every individual call is authorized and the position is healthy after repayment. [19](#0-18) 

Fees reduce but do not eliminate profitability when the oracle movement exceeds the combined issue, swap, and repay costs. The maximum issue amount can also be chosen to keep the post-update position exactly at its collateral-factor boundary until it is fully repaid. [20](#0-19) [21](#0-20) 

### Recommendation
Do not execute oracle-priced synthetic swaps against a price that can be predictably superseded in the same ordering window. A robust redesign should either disable `Pool.swap` or settle conversion requests at a future price that is not known when the request is submitted, such as a two-phase commit and later execution against the next valid oracle state.

If direct swaps remain, the protocol should bind the transaction to the oracle state visible to the caller, reject execution after a newer oracle state becomes available, and atomically include the oracle update before any state-changing conversion. Operators and user interfaces should submit the signed oracle update in the same transaction before `issue`, `swap`, `repay`, `withdraw`, or `liquidate`, where the oracle design permits it. Fees alone are not a complete mitigation because sufficiently large oracle movements remain profitable. [7](#0-6) 

### Proof of Concept
A Hardhat or Foundry fork test should arrange two registered synthetic assets with distinct oracle feeds, for example `msETH` and `msUSD`, a collateral token, and the corresponding `DepositToken` and `DebtToken` contracts. The public pull-oracle update path shown by the repository’s Hemi tests demonstrates that an oracle update can be invoked directly before protocol calls. [22](#0-21) 

The sequence is:

```solidity
// Initial state:
// WETH = $2,000, msETH = $2,000, msUSD = $1
// collateralFactor = 0.75e18

weth.approve(address(msdWETH), 1 ether);
msdWETH.deposit(1 ether, attacker);

// Borrows the maximum synthetic exposure.
msETHDebt.issue(0.75 ether, attacker);

// Front-run the oracle update.
pool.swap(msETH, msUSD, 0.75 ether);
// Attacker receives 1_500 msUSD.

// Execute the legitimate pending oracle update:
// ETH/msETH $2,000 -> $1,800.

// Back-run the update and buy back only the fixed debt units.
pool.swap(msUSD, msETH, 1_350 ether);
// Attacker receives 0.75 msETH and retains 150 msUSD.

msETHDebt.repayAll(attacker);
msdWETH.withdraw(1 ether, attacker);

assertEq(weth.balanceOf(attacker), initialWethBalance);
assertGt(msUSD.balanceOf(attacker), 0);
assertEq(msETHDebt.balanceOf(attacker), 0);
```

The retained `150 msUSD` is the extracted difference before swap, issue, repay, and withdrawal fees. The same transaction ordering can be expressed with `Operator.execute` for the protocol calls, while the legitimate public oracle-update transaction is sandwiched between the first and second `Pool.swap` calls. [16](#0-15) [2](#0-1)

### Citations

**File:** contracts/Pool.sol (L175-182)
```text
    function initialize(IPoolRegistry poolRegistry_) public initializer {
        if (address(poolRegistry_) == address(0)) revert PoolRegistryIsNull();
        __Pauseable_init();

        _poolRegistry = poolRegistry_;
        isSwapActive = true;
        maxLiquidable = 0.5e18; // 50%
    }
```

**File:** contracts/Pool.sol (L227-235)
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
```

**File:** contracts/Pool.sol (L262-265)
```text
        _debtInUsd = debtOf(account_);
        (_depositInUsd, _issuableLimitInUsd) = depositOf(account_);
        _isHealthy = _debtInUsd <= _issuableLimitInUsd;
        _issuableInUsd = _debtInUsd < _issuableLimitInUsd ? _issuableLimitInUsd - _debtInUsd : 0;
```

**File:** contracts/Pool.sol (L274-286)
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
```

**File:** contracts/Pool.sol (L493-524)
```text
        _amountIn = _poolRegistry.masterOracle().quote(
            address(syntheticTokenOut_),
            address(syntheticTokenIn_),
            amountOut_
        );
    }

    /**
     * @notice Quote `amountOut_` get from `amountIn_`
     * @param syntheticTokenIn_ Synth in
     * @param syntheticTokenOut_ Synth out
     * @param amountIn_ Amount in
     * @return _amountOut Amount out
     * @return _fee Fee to charge in `syntheticTokenOut_`
     */
    function quoteSwapOut(
        ISyntheticToken syntheticTokenIn_,
        ISyntheticToken syntheticTokenOut_,
        uint256 amountIn_
    ) public view override returns (uint256 _amountOut, uint256 _fee) {
        _amountOut = _poolRegistry.masterOracle().quote(
            address(syntheticTokenIn_),
            address(syntheticTokenOut_),
            amountIn_
        );

        uint256 _swapFee = feeProvider.swapFees(address(syntheticTokenIn_), address(syntheticTokenOut_));

        if (_swapFee > 0) {
            _fee = _amountOut.wadMul(_swapFee);
            _amountOut -= _fee;
        }
```

**File:** contracts/Pool.sol (L642-668)
```text
    function swap(
        ISyntheticToken syntheticTokenIn_,
        ISyntheticToken syntheticTokenOut_,
        uint256 amountIn_
    )
        external
        override
        whenNotShutdown
        nonReentrant
        onlyIfSyntheticTokenExists(syntheticTokenIn_)
        onlyIfSyntheticTokenExists(syntheticTokenOut_)
        returns (uint256 _amountOut, uint256 _fee)
    {
        address _msgSender = _msgSender();

        if (!isSwapActive) revert SwapFeatureIsInactive();
        if (amountIn_ == 0 || amountIn_ > syntheticTokenIn_.balanceOf(_msgSender)) revert AmountInIsInvalid();

        syntheticTokenIn_.burn(_msgSender, amountIn_);

        (_amountOut, _fee) = quoteSwapOut(syntheticTokenIn_, syntheticTokenOut_, amountIn_);

        if (_fee > 0) {
            syntheticTokenOut_.mint(_poolRegistry.feeCollector(), _fee);
        }

        syntheticTokenOut_.mint(_msgSender, _amountOut);
```

**File:** contracts/DebtToken.sol (L248-268)
```text
        accrueInterest();

        address _msgSender = _msgSender();
        IPool _pool = pool;
        ISyntheticToken _syntheticToken = syntheticToken;

        (, , , , uint256 _issuableInUsd) = _pool.debtPositionOf(_msgSender);

        IMasterOracle _masterOracle = _pool.masterOracle();

        if (amount_ > _masterOracle.quoteUsdToToken(address(_syntheticToken), _issuableInUsd)) {
            revert NotEnoughCollateral();
        }

        _mint(_pool, _masterOracle, _msgSender, amount_);

        (_issued, _fee) = quoteIssueOut(amount_);
        if (_fee > 0) {
            _syntheticToken.mint(_pool.feeCollector(), _fee);
        }
        _syntheticToken.mint(to_, _issued);
```

**File:** contracts/DebtToken.sol (L418-454)
```text
    function repay(
        address onBehalfOf_,
        uint256 amount_
    )
        external
        override
        whenNotShutdown
        nonReentrant
        onlyIfSyntheticTokenExists
        returns (uint256 _repaid, uint256 _fee)
    {
        if (amount_ == 0) revert AmountIsZero();

        accrueInterest();

        address _msgSender = _msgSender();
        IPool _pool = pool;
        ISyntheticToken _syntheticToken = syntheticToken;

        (_repaid, _fee) = quoteRepayOut(amount_);
        if (_fee > 0) {
            _syntheticToken.seize(_msgSender, _pool.feeCollector(), _fee);
        }

        uint256 _debtFloorInUsd = _pool.debtFloorInUsd();
        if (_debtFloorInUsd > 0) {
            uint256 _newDebtInUsd = _pool.masterOracle().quoteTokenToUsd(
                address(_syntheticToken),
                balanceOf(onBehalfOf_) - _repaid
            );
            if (_newDebtInUsd > 0 && _newDebtInUsd < _debtFloorInUsd) {
                revert RemainingDebtIsLowerThanTheFloor();
            }
        }

        _syntheticToken.burn(_msgSender, _repaid);
        _burn(onBehalfOf_, _repaid);
```

**File:** contracts/DepositToken.sol (L383-395)
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
```

**File:** contracts/DepositToken.sol (L536-551)
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
```

**File:** contracts/Operator.sol (L34-54)
```text
    function execute(
        Call[] calldata calls_
    ) external payable override nonReentrant setMsgSender returns (bytes[] memory _returnData) {
        uint256 _length = calls_.length;
        _returnData = new bytes[](_length);

        uint256 _sumOfValues;
        Call calldata _call;
        for (uint256 i; i < _length; ) {
            _call = calls_[i];
            uint256 _value = _call.value;
            unchecked {
                _sumOfValues += _value;
            }
            _returnData[i] = _call.target.functionCallWithValue(_call.callData, _value);
            unchecked {
                ++i;
            }
        }

        require(msg.value == _sumOfValues, "value-mismatch");
```

**File:** contracts/utils/SynthContext.sol (L14-23)
```text
    function _msgSender() internal view virtual override returns (address) {
        IPoolRegistry _poolRegistry = poolRegistry();
        if (address(_poolRegistry) != address(0)) {
            IOperator _operator = _poolRegistry.operator();
            if (msg.sender == address(_operator)) {
                return _operator.getActualMsgSender();
            }
        }

        return msg.sender;
```

**File:** test/E2E.hemi.pullOracle.test.ts (L117-126)
```typescript
      const wrappedPriceProvider = WrapperBuilder.wrap(pullPriceProvider).usingDataService({
        dataPackagesIds: ['ETH'],
      })
      // update price by directly calling priceProvider
      await wrappedPriceProvider.updatePrice([ETH_USD_FEED_ID])

      // then
      const priceInUsd = await masterOracle.getPriceInUsd(msETH.address)
      const {_depositInUsd} = await pool.debtPositionOf(alice.address)
      expect(_depositInUsd).eq(amount.mul(priceInUsd).div(parseEther('1')))
```
