### Title
Missing minimum-output protection allows oracle-price slippage in synthetic-token swaps - ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
`Pool.swap` fixes only `amountIn_` and provides no caller-specified `amountOutMin`, while the output is calculated dynamically from `MasterOracle` at execution time. [1](#0-0)  A public oracle update or other transaction ordering change can therefore cause a caller to receive materially less output than the value quoted when the transaction was created. [2](#0-1) 

### Finding Description
`quoteSwapOut` converts `syntheticTokenIn_` to `syntheticTokenOut_` using the current `MasterOracle` price and subtracts the configured swap fee. [3](#0-2)  `swap` validates that the feature is active and the caller has enough input tokens, burns the full `amountIn_`, calculates the output at execution time, and mints whatever amount is returned without any minimum-output check. [4](#0-3) 

The function is externally reachable through the public `swap(ISyntheticToken,ISyntheticToken,uint256)` entry point and can also be reached through `Operator.execute` because `Pool` resolves the sender through `SynthContext`. [5](#0-4)  The deployed pull-oracle path can be refreshed through Pyth’s public `updatePriceFeeds` function, meaning an ordinary account can order a valid price update before a pending swap. [6](#0-5) 

For example, a user submits `swap(msETH, msUSD, 1e18)` after observing a quote for approximately 2,000 msUSD. If a valid oracle update lowering the msETH price executes first, the same transaction still burns the full 1 msETH but mints the new, lower msUSD amount without reverting. [7](#0-6) 

### Impact Explanation
The user irreversibly burns the input synthetic token and receives an output amount determined by a later oracle state rather than the price used to construct the transaction. [7](#0-6)  Sufficient adverse movement can reduce the output to dust or zero while still consuming the nonzero input, resulting in a direct loss of user funds. [4](#0-3) 

The existing checks only enforce nonzero input, caller balance, registered synthetic tokens, active swap status, shutdown state, and reentrancy protection; none constrains execution-time output. [8](#0-7) 

### Likelihood Explanation
The vulnerable function is public and only requires the caller to own a registered synthetic token while swaps are active. [9](#0-8)  Pull-oracle updates are permissionless, so any pending valid update can be included before a user’s swap, and ordinary transaction ordering during volatile market periods can produce the same result without privileged access. [6](#0-5)  The repository’s tests explicitly exercise a pull-oracle deployment where `Pyth.updatePriceFeeds` is called by a regular signer before interacting with the protocol. [10](#0-9) 

### Recommendation
Add an `amountOutMin_` parameter to `IPool.swap` and `Pool.swap`, calculate the quote before burning the input, and revert when the calculated `_amountOut` is below the caller’s bound. [1](#0-0)  The same bound should be propagated through `Operator.execute` call data so users employing the operator retain identical protection. [5](#0-4) 

### Proof of Concept
The following Hardhat fork test demonstrates the transaction-ordering issue using the deployed pull oracle and the publicly callable Pyth update function; `priceUpdate` is the signed update data fetched from the configured Hermes endpoint. [6](#0-5) 

```typescript
// test/E2E.mainnet.swap-slippage.test.ts
import {expect} from 'chai'
import {ethers} from 'hardhat'
import {parseEther} from '../helpers'
import Address from '../helpers/address'
import {EvmPriceServiceConnection} from '@pythnetwork/pyth-evm-js'

describe('Pool.swap oracle slippage', function () {
  it('accepts a lower output after a permissionless oracle update', async function () {
    const [, victim, relayer] = await ethers.getSigners()

    const pool = await ethers.getContractAt(
      'IPool',
      '0x574a32f1047C631653D9283d36e73cF9BA67B940'
    )
    const msETH = await ethers.getContractAt('IERC20', Address.MSETH_ADDRESS)
    const msUSD = await ethers.getContractAt('IERC20', Address.MSUSD_ADDRESS)

    const amountIn = parseEther('1')

    // Give the victim an input balance on the fork.
    await ethers.provider.send('hardhat_setBalance', [
      victim.address,
      ethers.utils.hexValue(parseEther('1')),
    ])
    await ethers.provider.send('hardhat_impersonateAccount', [
      Address.MSETH_WHALE,
    ])
    const whale = await ethers.getSigner(Address.MSETH_WHALE)
    await msETH.connect(whale).transfer(victim.address, amountIn)

    // Quote at the old oracle state.
    const quoted = await pool.quoteSwapOut(
      msETH.address,
      msUSD.address,
      amountIn
    )
    expect(quoted._amountOut).to.be.gt(0)

    // Any account may submit the valid signed Pyth update first.
    const pyth = await ethers.getContractAt(
      [
        'function getUpdateFee(bytes[] calldata) view returns (uint256)',
        'function updatePriceFeeds(bytes[] calldata) payable',
      ],
      '0x4305fb66699c3b2702d4d05cf36551390a4c69c6',
      relayer
    )

    const pythAPI = new EvmPriceServiceConnection(
      'https://hermes.pyth.network'
    )
    const msEthFeed =
      '0xff61491a931112ddf1bd8147cd1b641375f79f5825126d665480874634fd0ace'
    const updateData = await pythAPI.getPriceFeedsUpdateData([msEthFeed])
    const fee = await pyth.getUpdateFee(updateData)
    await pyth.updatePriceFeeds(updateData, {value: fee})

    const outputBefore = await msUSD.balanceOf(victim.address)
    const inputBefore = await msETH.balanceOf(victim.address)

    await pool
      .connect(victim)
      .swap(msETH.address, msUSD.address, amountIn)

    const actualOut = (await msUSD.balanceOf(victim.address)).sub(outputBefore)

    // Full input was consumed, but the output is determined by the new price.
    expect(await msETH.balanceOf(victim.address)).to.equal(
      inputBefore.sub(amountIn)
    )
    expect(actualOut).to.be.lt(quoted._amountOut)
  })
})
```

A `minAmountOut_` parameter would make this testable directly by causing the second transaction to revert when `actualOut < quoted._amountOut`; currently, the API gives the user no way to express that bound. [1](#0-0)

### Citations

**File:** contracts/interfaces/IPool.sol (L100-104)
```text
    function swap(
        ISyntheticToken syntheticTokenIn_,
        ISyntheticToken syntheticTokenOut_,
        uint256 amountIn_
    ) external returns (uint256 _amountOut, uint256 _fee);
```

**File:** contracts/Pool.sol (L508-523)
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

**File:** test/E2E.mainnet.test.ts (L971-984)
```typescript
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

        // Setup
```

**File:** test/E2E.mainnet.test.ts (L1016-1029)
```typescript
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
