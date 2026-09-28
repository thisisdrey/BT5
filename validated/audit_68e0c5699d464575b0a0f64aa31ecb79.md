### Title
Oracle-latency synthetic swaps allow collateral extraction without updating debt denomination - (contracts/Pool.sol)

### Summary
`Pool.swap()` is a public, zero-slippage synthetic-token exchange that burns `syntheticTokenIn_` and mints `syntheticTokenOut_` solely at the current `MasterOracle` quote. [1](#0-0)  The swap does not modify the caller’s debt-token position, so an attacker can change the denomination of circulating synthetic assets while their obligation remains denominated in the original, depreciating asset. [2](#0-1) 

### Finding Description
`quoteSwapOut()` converts `amountIn_` directly through `masterOracle().quote()` and deducts only the configured directional `swapFees` value. [3](#0-2)  Unconfigured swap pairs default to a zero fee because `swapFees` is an ordinary mapping with no fallback fee. [4](#0-3) 

A borrower can deposit collateral, issue synthetic asset A, and receive the synthetic tokens while the debt remains recorded in `DebtToken` A. [5](#0-4)  If A’s oracle price is about to decrease, the borrower can front-run the update and call `Pool.swap(msA, msB, amount)` while A is still quoted at its stale, higher price. [1](#0-0) 

The resulting msB is freely held by the attacker, but `debtOf()` continues valuing only the attacker’s `DebtToken` A balance. [2](#0-1)  After the A price update is executed, `debtPositionOf()` reports less USD debt and `unlockedBalanceOf()` makes additional collateral withdrawable. [6](#0-5) [7](#0-6)  `DepositToken.withdraw()` then burns deposit tokens and pulls real collateral from `Treasury`. [8](#0-7) [9](#0-8) 

### Impact Explanation
This breaks the protocol’s solvency invariant: the USD value of debt tokens no longer covers the USD value of circulating synthetic liabilities after a swap across assets whose prices subsequently diverge. [10](#0-9) [11](#0-10) 

For example, with collateral C worth `$2`, a `50%` collateral factor, `msA = $1`, and `msB = $1`, an attacker deposits `$2` and issues `1 msA`. [12](#0-11) [13](#0-12)  Before an oracle update reduces A to `$0.50`, the attacker swaps `1 msA` for approximately `1 msB`; after the update, the recorded debt is only `$0.50`, unlocking `$1` of collateral while the attacker retains `$1` of msB. [7](#0-6) 

The attacker can abandon the remaining debt position after selling or redeeming msB elsewhere, leaving the protocol with a synthetic liability that is not matched by the reduced A-denominated debt. [2](#0-1)  The same mechanism also functions as a zero-slippage oracle-priced exchange for unrelated synthetic-token holders that buy a depreciated synthetic asset externally and swap it at its stale oracle price. [3](#0-2) 

### Likelihood Explanation
Any unprivileged holder of a registered synthetic token can invoke `swap()` because its checks are limited to swap activation, a nonzero amount, sufficient token balance, registered synthetic tokens, and the pool not being shut down. [14](#0-13)  The attack requires only a predictable oracle update or temporary oracle latency whose relative price movement exceeds the directional swap fee. [15](#0-14) 

Deployment scripts configure fees for the `msUSD`/`msETH` directions, while other registered synthetic-token directions can remain at the mapping’s zero default unless separately configured. [16](#0-15) [4](#0-3)  The deployed `Pool` initializes swaps as active by default. [17](#0-16) 

### Recommendation
Do not allow a swap to change the denomination of a circulating synthetic asset while leaving the borrower’s debt in the input asset. [11](#0-10)  For borrowers, convert the corresponding `DebtToken` A debt into `DebtToken` B debt during the swap, up to the swapped amount, or require borrowers to repay A and separately issue B. [18](#0-17) [5](#0-4) 

Additionally, require strict oracle freshness, conservative bounds or TWAP pricing for synthetic swaps, and ensure every enabled direction has a nonzero risk-appropriate fee rather than relying on the zero-value mapping default. [15](#0-14) [4](#0-3) 

### Proof of Concept
A Foundry fork or Hardhat test can reproduce the issue with the following sequence:

1. Configure a pool with collateral `C`, synthetic assets `A` and `B`, collateral factor `0.5e18`, and `swapFees[A][B] == 0`. [4](#0-3) 
2. Set oracle prices to `C = $2`, `A = $1`, and `B = $1`.
3. Attacker calls `depositTokenC.deposit(1 C, attacker)`, receiving deposit shares backed by `$2`. [12](#0-11) 
4. Attacker calls `debtTokenA.issue(1e18, attacker)`, receiving `1 msA` and recording `1` unit of A-denominated debt. [5](#0-4) 
5. Front-running an A price update from `$1` to `$0.50`, call `pool.swap(msA, msB, 1e18)`; the call burns `1 msA` and mints approximately `1 msB` while leaving the attacker’s debt in A. [1](#0-0) 
6. Apply the oracle update and call `pool.debtPositionOf(attacker)`; the debt is now valued at `$0.50` while the collateral remains worth `$2`. [6](#0-5) 
7. `depositTokenC.unlockedBalanceOf(attacker)` permits withdrawal of `$1` of collateral because the remaining `$1` deposit at a `50%` collateral factor exactly covers the `$0.50` debt. [7](#0-6) 
8. Call `depositTokenC.withdraw(unlocked, attacker)` to extract the collateral. [8](#0-7) 
9. Assert that the attacker retains `1 msB`, withdrew `$1` of collateral, and still has only `$0.50` of recorded debt; this demonstrates a synthetic-liability/debt mismatch and protocol undercollateralization. [2](#0-1)

### Citations

**File:** contracts/Pool.sol (L175-181)
```text
    function initialize(IPoolRegistry poolRegistry_) public initializer {
        if (address(poolRegistry_) == address(0)) revert PoolRegistryIsNull();
        __Pauseable_init();

        _poolRegistry = poolRegistry_;
        isSwapActive = true;
        maxLiquidable = 0.5e18; // 50%
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

**File:** contracts/Pool.sol (L248-265)
```text
    function debtPositionOf(
        address account_
    )
        public
        view
        override
        returns (
            bool _isHealthy,
            uint256 _depositInUsd,
            uint256 _debtInUsd,
            uint256 _issuableLimitInUsd,
            uint256 _issuableInUsd
        )
    {
        _debtInUsd = debtOf(account_);
        (_depositInUsd, _issuableLimitInUsd) = depositOf(account_);
        _isHealthy = _debtInUsd <= _issuableLimitInUsd;
        _issuableInUsd = _debtInUsd < _issuableLimitInUsd ? _issuableLimitInUsd - _debtInUsd : 0;
```

**File:** contracts/Pool.sol (L508-524)
```text
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

**File:** contracts/storage/FeeProviderStorage.sol (L67-72)
```text
abstract contract FeeProviderStorageV2 is FeeProviderStorageV1 {
    /**
     * @notice The fees charged when swapping synthetic tokens (by synthIn => synthOut directions)
     * @dev Use 18 decimals (e.g. 1e16 = 1%)
     */
    mapping(address => mapping(address => uint256)) public override swapFees;
```

**File:** contracts/DebtToken.sol (L235-270)
```text
    function issue(
        uint256 amount_,
        address to_
    )
        external
        override
        whenNotShutdown
        nonReentrant
        onlyIfSyntheticTokenExists
        returns (uint256 _issued, uint256 _fee)
    {
        if (amount_ == 0) revert AmountIsZero();

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

        emit SyntheticTokenIssued(_msgSender, to_, amount_, _issued, _fee);
```

**File:** contracts/DebtToken.sol (L453-454)
```text
        _syntheticToken.burn(_msgSender, _repaid);
        _burn(onBehalfOf_, _repaid);
```

**File:** contracts/DepositToken.sol (L211-235)
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

**File:** contracts/DepositToken.sol (L406-411)
```text
    function withdraw(uint256 amount_, address to_) external override returns (uint256 _withdrawn, uint256 _fee) {
        if (to_ == address(0)) revert RecipientIsNull();
        address _msgSender = _msgSender();
        _revertIfLocked(_msgSender, amount_);

        return _withdraw({account_: _msgSender, amount_: amount_, to_: to_});
```

**File:** contracts/Treasury.sol (L66-71)
```text
    function pull(address to_, uint256 amount_) external override nonReentrant {
        address _msgSender = _msgSender();
        if (!pool.doesDepositTokenExist(IDepositToken(_msgSender))) revert SenderIsNotDepositToken();
        if (to_ == address(0)) revert RecipientIsNull();
        if (amount_ == 0) revert AmountIsZero();
        IDepositToken(_msgSender).underlying().safeTransfer(to_, amount_);
```

**File:** deploy/scripts/mainnet/pool1/11_fee_provider.ts (L33-52)
```typescript
  const {address: msUSD} = await get('MsUSDSynthetic')
  const {address: msETH} = await get('MsETHSynthetic')

  await updateParamIfNeeded(hre, {
    contractAlias: FeeProvider_Pool1,
    readMethod: 'swapFees',
    readArgs: [msUSD, msETH],
    writeMethod: 'updateSwapFee',
    writeArgs: [msUSD, msETH, parseEther('0.01').toString()], // 1%
    isCurrentValueUpdated: (current, writeArgs) => current.toString() === writeArgs[2].toString(),
  })

  await updateParamIfNeeded(hre, {
    contractAlias: FeeProvider_Pool1,
    readMethod: 'swapFees',
    readArgs: [msETH, msUSD],
    writeMethod: 'updateSwapFee',
    writeArgs: [msETH, msUSD, parseEther('0.0055').toString()], // 0.55%
    isCurrentValueUpdated: (current, writeArgs) => current.toString() === writeArgs[2].toString(),
  })
```
