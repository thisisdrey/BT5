### Title
Stale-to-fresh pull-oracle updates enable same-transaction synthetic swap arbitrage - ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
`Pool.swap()` burns one synthetic asset and mints another using the current `IMasterOracle.quote()` value, without binding that quote to a single oracle update or a distinct quote epoch. [1](#0-0)  When Metronome is configured to consume a public pull oracle, an unprivileged caller can submit an older valid signed price update, swap at that price, submit a newer valid update, and swap back at the newer price in one transaction through `Operator.execute()`. [2](#0-1) [3](#0-2) 

### Finding Description
`Pool.swap()` is permissionless apart from requiring both tokens to be registered, swaps to be active, and the caller to own the input synthetic balance. [4](#0-3)  The function burns `syntheticTokenIn_`, calls `quoteSwapOut()`, and mints the oracle-quoted amount of `syntheticTokenOut_` plus any configured fee. [5](#0-4)  `quoteSwapOut()` forwards directly to `PoolRegistry.masterOracle().quote()` and therefore reflects whatever oracle state exists at that exact point in the transaction. [6](#0-5) 

`Operator.execute()` can atomically call the public pull-oracle update endpoint, call `Pool.swap()`, call the update endpoint again, and call `Pool.swap()` a second time. [2](#0-1)  `SynthContext._msgSender()` maps calls made through the configured operator back to the original EOA, so both swaps operate on the attacker’s synthetic balances rather than the operator’s balances. [3](#0-2)  Neither `Pool.swap()` nor `SyntheticToken.mint()`/`burn()` records or validates an oracle-update block, timestamp, sequence, or transaction-local quote epoch. [1](#0-0) [7](#0-6) 

### Impact Explanation
During a favorable price move, the attacker can buy the appreciating synthetic asset under the older quote and sell it under the newer quote while remaining within the stale-data limits accepted by the configured oracle. [8](#0-7)  Each individual swap appears internally consistent, but the two swaps observe different oracle states in one transaction, allowing the ending synthetic balance to exceed the starting balance without the attacker supplying additional collateral. [5](#0-4)  Because the newly minted synthetic tokens are not matched by newly issued borrower debt or collateral, repeated extraction increases unbacked synthetic supply and can move the pool toward insolvency. [5](#0-4) 

### Likelihood Explanation
The attack is reachable whenever a configured pull oracle is stale enough to accept an older signed update while a newer signed update is also available, and the price difference exceeds swap fees, oracle update fees, and transaction costs. [9](#0-8)  Metronome’s own fork tests demonstrate the intended pattern of publicly updating pull-oracle prices and atomically using the refreshed price through `Operator.execute()`. [10](#0-9)  The deployed code does not add a protocol-level check that all quotes used during a state-changing operation came from the same oracle update or that the pull oracle changed only once in the block. [6](#0-5) [1](#0-0) 

### Recommendation
Extend the oracle integration so price-consuming operations can verify a monotonic update epoch and, for pull-oracle assets, either require the relevant feed to have been updated in the current block or reject more than one feed update per block. [11](#0-10)  A practical design is to expose `updatedAtBlock` or an equivalent sequence number from the oracle adapter, cache it when a pull price is accepted, and make `Pool.swap()`, liquidation quotes, and health calculations reject quotes associated with an update already consumed in the same block or transaction epoch. [12](#0-11) [6](#0-5)  The mitigation should be enforced by the oracle adapter or `PoolRegistry` rather than trusting caller-provided timestamps, because `Pool.swap()` currently receives no quote-context parameter and always reads live oracle state. [13](#0-12) [1](#0-0) 

### Proof of Concept
A reproducible fork test should use the deployed mainnet `Pool` at `0x0078253265Ca73EB2e81D20920365995F63F7bf8`, the configured Pyth pull oracle and price service, and two valid signed updates for the ETH/msETH feed with publish times `t1 < t2`, where both updates satisfy the configured freshness window and `t1` is newer than the feed’s current on-chain publish time. [14](#0-13) [15](#0-14) 

```solidity
// SPDX-License-Identifier: UNLICENSED
pragma solidity ^0.8.24;

import "forge-std/Test.sol";

interface IPyth {
    function getUpdateFee(bytes[] calldata) external view returns (uint256);
    function updatePriceFeeds(bytes[] calldata) external payable;
}

interface IPool {
    function swap(address tokenIn, address tokenOut, uint256 amountIn)
        external
        returns (uint256 amountOut, uint256 fee);
}

interface IERC20 {
    function balanceOf(address) external view returns (uint256);
}

contract PullOracleSwapArbTest is Test {
    address constant PYTH = 0x4305fB66699C3B2702D4d05Cf36551390A4c69C6;
    address constant POOL = 0x0078253265Ca73EB2e81D20920365995F63F7bf8;

    address attacker = address(0xA11CE);
    address msUSD;
    address msETH;

    function test_sameTxOracleArbitrage() external {
        vm.createSelectFork(vm.envString("MAINNET_RPC_URL"), PINNED_STALE_BLOCK);

        msUSD = /* deployed msUSD token */;
        msETH = /* deployed msETH token */;

        deal(msUSD, attacker, 1_000_000e18);

        bytes[] memory oldUpdate = fetchValidPythUpdateAt(PINNED_STALE_BLOCK, T1);
        bytes[] memory newUpdate = fetchValidPythUpdateAt(PINNED_STALE_BLOCK, T2);

        uint256 fee = IPyth(PYTH).getUpdateFee(oldUpdate)
            + IPyth(PYTH).getUpdateFee(newUpdate);
        vm.deal(attacker, fee);
        vm.startPrank(attacker);

        // Advance the stale oracle only to the older valid quote.
        IPyth(PYTH).updatePriceFeeds{value: IPyth(PYTH).getUpdateFee(oldUpdate)}(
            oldUpdate
        );

        (uint256 bought, ) = IPool(POOL).swap(
            msUSD,
            msETH,
            IERC20(msUSD).balanceOf(attacker)
        );

        // Advance the same feed again to the newer valid quote in this tx.
        IPyth(PYTH).updatePriceFeeds{value: IPyth(PYTH).getUpdateFee(newUpdate)}(
            newUpdate
        );

        (uint256 finalMsUSD, ) = IPool(POOL).swap(msETH, msUSD, bought);

        vm.stopPrank();

        assertGt(finalMsUSD, 1_000_000e18);
    }
}
```

For an `Operator.execute()` variant, encode the four calls as `updatePriceFeeds(oldUpdate)`, `swap(msUSD, msETH, amount)`, `updatePriceFeeds(newUpdate)`, and `swap(msETH, msUSD, bought)`; `SynthContext` makes both swaps debit and credit the original caller rather than the operator contract. [2](#0-1) [3](#0-2)  The oracle updates must use genuine signed data rather than malformed or unauthorized price data, while the proof asserts that the second swap realizes the price movement as profit. [16](#0-15) [5](#0-4)

### Citations

**File:** contracts/Pool.sol (L456-471)
```text
        _toLiquidator = masterOracle().quote(
            address(syntheticToken_),
            address(depositToken_.underlying()),
            amountToRepay_
        );

        (uint128 _liquidatorIncentive, uint128 _protocolFee) = feeProvider.liquidationFees();

        if (_protocolFee > 0) {
            _fee = _toLiquidator.wadMul(_protocolFee);
        }
        if (_liquidatorIncentive > 0) {
            _toLiquidator += _toLiquidator.wadMul(_liquidatorIncentive);
        }

        _totalToSeize = _fee + _toLiquidator;
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

**File:** contracts/Pool.sol (L601-603)
```text
    function masterOracle() public view override returns (IMasterOracle) {
        return _poolRegistry.masterOracle();
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

**File:** contracts/Operator.sol (L34-55)
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
    }
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

**File:** contracts/SyntheticToken.sol (L187-219)
```text
    function burn(address from_, uint256 amount_) external override onlyIfCanBurn {
        _burn(from_, amount_);
    }

    /**
     * @notice Atomically decrease the allowance granted to `spender` by the caller
     */
    function decreaseAllowance(address spender_, uint256 subtractedValue_) external returns (bool) {
        address _msgSender = _msgSender();
        uint256 _currentAllowance = allowance[_msgSender][spender_];
        if (_currentAllowance < subtractedValue_) revert DecreasedAllowanceBelowZero();
        unchecked {
            _approve(_msgSender, spender_, _currentAllowance - subtractedValue_);
        }
        return true;
    }

    /**
     * @notice Atomically increase the allowance granted to `spender` by the caller
     */
    function increaseAllowance(address spender_, uint256 addedValue_) external returns (bool) {
        address _msgSender = _msgSender();
        _approve(_msgSender, spender_, allowance[_msgSender][spender_] + addedValue_);
        return true;
    }

    /**
     * @notice Mint synthetic token
     * @param to_ The account to mint to
     * @param amount_ The amount to mint
     */
    function mint(address to_, uint256 amount_) external override onlyIfCanMint {
        _mint(to_, amount_);
```

**File:** test/E2E.mainnet.next.test.ts (L833-869)
```typescript
      const PYTH_PROTOCOL = '0x4305fb66699c3b2702d4d05cf36551390a4c69c6'
      const PYTH_PROVIDER = '0x7c2d5b1E7d7BE588389BDb94138cC37dC014e85c'
      const PYTH_ORACLE = '0x1f278B7EFf04ADd48Ff81ae1a01cBC178b3dD351'

      // Contracts
      let pyth: Contract
      let pythProvider: Contract
      let pullOracle: Contract

      // Feeds
      const pythFeedIds = [
        '0xeaa020c61cc479712813461ce153894a96a6c00b21ed0cfc2798d1f9a9e9c94a', // USDC
        '0xb0948a5e5313200c632b51bb5ca32f6de0d36e9950a942d19751e833f70dabfd', // DAI
        '0xff61491a931112ddf1bd8147cd1b641375f79f5825126d665480874634fd0ace', // WETH,msETH
        '0xc3d5d8d6d17081b3d0bbca6e2fa3a6704bb9a9561d9f9e1dc52db47629f862ad', // FRAX
        '0xb2bb466ff5386a63c18aa7c3bc953cb540c755e2aa99dafb13bc4c177692bed0', // sfrxETH
        '0xa0255134973f4fdf2f8f7808354274a3b1ebc6ee438be898d045e8b56ba1fe13', // rETH
        '0x846ae1bdb6300b817cee5fdee2a6da192775030db5615b94a465f53bd40850b5', // stETH
        '0xe62df6c8b4a85fe1a67db44dc12de5db330f7ac66b72dc658afedf0f4a415b43', // WBTC,msBTC
        '0x15ecddd26d49e1a8f1de9376ebebc03916ede873447c1255d2d5891b92ce5717', // cbETH
      ]
      const pythAPI = new EvmPriceServiceConnection('https://hermes.pyth.network')

      beforeEach(async function () {
        // Initialize contracts
        const pythABI = [
          'function getUpdateFee(bytes[] calldata updateData) external view returns (uint feeAmount)',
          'function updatePriceFeeds(bytes[] calldata updateData) external payable',
        ]
        const providerABI = [
          'function getPriceInUsd(address token_) external view returns (uint256 _priceInUsd, uint256 _lastUpdatedAt)',
        ]
        const oracleABI = ['function getPriceInUsd(address) view returns (uint256 _priceInUsd)']

        pyth = await ethers.getContractAt(pythABI, PYTH_PROTOCOL, alice)
        pythProvider = await ethers.getContractAt(providerABI, PYTH_PROVIDER, alice)
        pullOracle = await ethers.getContractAt(oracleABI, PYTH_ORACLE, alice)
```

**File:** test/E2E.mainnet.next.test.ts (L879-916)
```typescript
      describe('when pull oracle prices are updated', function () {
        beforeEach(async function () {
          // Update all feeds
          const priceUpdate = await pythAPI.getPriceFeedsUpdateData(pythFeedIds)
          const fee = await pyth.getUpdateFee(pythFeedIds)
          await pyth.updatePriceFeeds(priceUpdate, {value: fee})
        })

        it('should get prices from provider', async function () {
          const {_priceInUsd: pythPrice} = await pythProvider.getPriceInUsd(Address.WETH_ADDRESS)
          expect(pythPrice).gt(0)
        })

        it('should get prices from oracle', async function () {
          // given
          const {_priceInUsd: pythPrice} = await pythProvider.getPriceInUsd(Address.CBETH_ADDRESS)
          expect(pythPrice).gt(0)

          // when
          const oraclePrice = await pullOracle.getPriceInUsd(Address.CBETH_ADDRESS)
          expect(oraclePrice).eq(pythPrice)
        })
      })

      describe('when pull-oracle is the default oracle', function () {
        beforeEach(async function () {
          await masterOracle.updateDefaultOracle(pullOracle.address)
          await masterOracle.updateTokenOracle(Address.STETH_ADDRESS, ethers.constants.AddressZero)
          await masterOracle.updateTokenOracle(Address.RETH_ADDRESS, ethers.constants.AddressZero)
          await masterOracle.updateTokenOracle(Address.CBETH_ADDRESS, ethers.constants.AddressZero)
        })

        describe('when pull oracle prices are updated', function () {
          beforeEach(async function () {
            // Update all feeds
            const priceUpdate = await pythAPI.getPriceFeedsUpdateData(pythFeedIds)
            const fee = await pyth.getUpdateFee(pythFeedIds)
            await pyth.updatePriceFeeds(priceUpdate, {value: fee})
```

**File:** test/E2E.mainnet.next.test.ts (L938-964)
```typescript
        describe('when pull oracle prices are outdated', function () {
          it('should interact with synth protocol', async function () {
            // given
            await expect(masterOracle.getPriceInUsd(usdc.address)).revertedWith('price-too-behind')

            // when
            const priceUpdate = await pythAPI.getPriceFeedsUpdateData(pythFeedIds)
            const fee = await pyth.getUpdateFee(pythFeedIds)

            const amount = parseUnits('100', 6)
            const calls: IOperator.CallStruct[] = [
              {
                target: pyth.address,
                value: fee,
                callData: pyth.interface.encodeFunctionData('updatePriceFeeds', [priceUpdate]),
              },
              {
                target: msdUSDC_1.address,
                value: 0,
                callData: msdUSDC_1.interface.encodeFunctionData('deposit', [amount, alice.address]),
              },
            ]
            const tx = () => operator.connect(alice).execute(calls, {value: fee})

            // then
            await expect(tx).changeTokenBalance(msdUSDC_1, alice, amount)
          })
```

**File:** contracts/interfaces/external/IMasterOracle.sol (L5-10)
```text
interface IMasterOracle {
    function quoteTokenToUsd(address _asset, uint256 _amount) external view returns (uint256 _amountInUsd);

    function quoteUsdToToken(address _asset, uint256 _amountInUsd) external view returns (uint256 _amount);

    function quote(address _assetIn, address _assetOut, uint256 _amountIn) external view returns (uint256 _amountOut);
```

**File:** deployments/mainnet/Pool.json (L1-2)
```json
{
  "address": "0x0078253265Ca73EB2e81D20920365995F63F7bf8",
```
