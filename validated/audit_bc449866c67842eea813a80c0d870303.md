### Title
Partial Vesper withdrawals strand users’ remaining `vToken` collateral in `VesperGateway` - ([contracts/VesperGateway.sol](contracts/VesperGateway.sol)) [1](#0-0) 

### Summary
`VesperGateway.withdraw()` burns the user’s full requested `msdToken` position before redeeming the corresponding Vesper shares, but it does not verify that Vesper actually consumed all of those shares. [2](#0-1) 

Metronome already recognizes that `IVPool.withdraw(shares)` may withdraw less than requested and burn fewer shares, because `AMO._withdrawFromVesper()` measures the actual token balance delta rather than assuming the requested amount was received. [3](#0-2) 

### Finding Description
A user first transfers `amount_` `msdTokens` to `VesperGateway`, then calls `DepositToken.withdraw(amount_, address(this))`, which removes the user’s entire Metronome collateral claim and sends the net `vToken` amount to the gateway. [4](#0-3) 

`DepositToken._withdraw()` completes the internal withdrawal by burning `_withdrawn` `msdTokens` and pulling the same quantity of underlying `vTokens` from `Treasury`. [5](#0-4) 

The gateway then calls `vToken_.withdraw(_vTokenAmount)` and forwards only the measured underlying-token balance delta to the user. [6](#0-5) 

If Vesper returns less underlying because it burns only part of the supplied shares, the unburned `vToken` balance remains in `VesperGateway`, while the user’s corresponding `msdToken` collateral has already been burned. [7](#0-6) 

The gateway has no user-accessible recovery path for those residual shares; `TokenHolder.sweep()` is permissioned through `_requireCanSweep()`, which `VesperGateway` restricts to the governor. [8](#0-7) [9](#0-8) 

### Impact Explanation
A partial Vesper withdrawal permanently disconnects the user from the portion of their `vToken` collateral that Vesper did not redeem. [7](#0-6) 

The user receives only the underlying amount actually paid by Vesper, loses the full Metronome deposit-token claim, and cannot later recover the residual `vTokens` left in the gateway when Vesper liquidity returns. [10](#0-9) 

This breaks collateral-withdrawal conservation and produces permanent freezing of user collateral, matching the report’s bug class rather than constituting a mere payout-difference issue. [7](#0-6) 

### Likelihood Explanation
The vulnerable path is public and requires only a registered `Pool`, a supported Vesper `vToken`, user-approved `msdTokens`, and an unlocked withdrawal amount. [11](#0-10) 

Metronome deploys Vesper-backed deposit tokens such as `vaETH` and `vaUSDC`, so the external partial-withdrawal behavior applies to production collateral types. [12](#0-11) [13](#0-12) 

The trigger is a Vesper liquidity shortfall or another condition under which `withdraw()` redeems fewer shares than supplied, which the codebase explicitly treats as possible behavior. [14](#0-13) 

No governor, keeper, privileged caller, malicious oracle, or trusted bridge participant is required; an ordinary caller reaches the path directly through `VesperGateway.withdraw()`. [1](#0-0) 

### Recommendation
Measure the gateway’s `vToken` balance before and after `vToken_.withdraw(_vTokenAmount)` and revert unless the share-balance decrease equals `_vTokenAmount`. [6](#0-5) 

Alternatively, redeposit or otherwise return the unburned `vToken` remainder to the caller so the caller retains a Metronome collateral claim equal to the unredeemed portion. [10](#0-9) 

A minimum-underlying-out parameter can additionally expose Vesper redemption slippage directly to the caller, but share-balance verification or remainder restoration is required to prevent stranded collateral. [7](#0-6) 

### Proof of Concept
The following Foundry-style reproduction models Vesper’s documented partial redemption by burning only the shares covered by available liquidity; against a registered `DepositToken`, the user ends with no `msdToken` claim while unburned `vTokens` remain in the gateway. [15](#0-14) [10](#0-9) 

```solidity
contract PartialVPool is ERC20, IVPool {
    IERC20 public token;

    constructor(IERC20 token_) ERC20("vUSDC", "vUSDC") {
        token = token_;
    }

    function deposit(uint256 amount_) external {
        token.transferFrom(msg.sender, address(this), amount_);
        _mint(msg.sender, amount_);
    }

    // Mirrors the behavior acknowledged by AMO._withdrawFromVesper():
    // fewer shares are burned when less underlying is available.
    function withdraw(uint256 shares_) external {
        uint256 liquidity = token.balanceOf(address(this));
        uint256 paid = liquidity < shares_ ? liquidity : shares_;

        _burn(msg.sender, paid);
        token.transfer(msg.sender, paid);
    }

    function poolRewards() external view returns (address) {}
    function pricePerShare() external view returns (uint256) {}
}

function testPartialVesperWithdrawStrandsUserShares() public {
    // Setup:
    // 1. Deploy/register DepositToken whose underlying is PartialVPool.
    // 2. Deposit 100 vTokens through DepositToken and receive 100 msdTokens.
    // 3. Leave only 40 underlying tokens in PartialVPool.

    uint256 msdAmount = 100 ether;

    msdToken.approve(address(vesperGateway), msdAmount);
    vesperGateway.withdraw(pool, partialVPool, msdAmount);

    // Full Metronome claim was consumed.
    assertEq(msdToken.balanceOf(user), 0);

    // Vesper paid only the available 40 underlying.
    assertEq(underlying.balanceOf(user), 40 ether);

    // The other 60 shares were not burned by Vesper, but are stranded.
    assertEq(partialVPool.balanceOf(address(vesperGateway)), 60 ether);
}
```

On a mainnet or Optimism fork, the same assertion can be produced by using a deployed Vesper-backed `DepositToken`, seeding the user through `VesperGateway.deposit()`, reducing the real Vesper pool’s immediately available underlying through ordinary public withdrawals, and then calling `VesperGateway.withdraw()`. [16](#0-15) [3](#0-2)

### Citations

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

**File:** contracts/VesperGateway.sol (L100-102)
```text
    /// @inheritdoc TokenHolder
    // solhint-disable-next-line no-empty-blocks
    function _requireCanSweep() internal view override onlyGovernor {}
```

**File:** contracts/AMO.sol (L137-147)
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

**File:** contracts/utils/TokenHolder.sol (L37-44)
```text
    function sweep(IERC20 token_, address to_, uint256 amount_) external {
        _requireCanSweep();

        if (address(token_) == address(0)) {
            Address.sendValue(payable(to_), amount_);
        } else {
            token_.safeTransfer(to_, amount_);
        }
```

**File:** deploy/scripts/mainnet/pool1/16_vaeth_deposit_token.ts (L11-18)
```typescript
const func = buildDepositTokenDeployFunction({
  poolAlias: Pool1,
  underlyingAddress: VAETH_ADDRESS,
  underlyingSymbol: 'vaETH',
  underlyingDecimals: 18,
  collateralFactor: parseEther('0.6'), // 60%
  maxTotalSupply: parseEther('4170'),
})
```

**File:** deploy/scripts/mainnet/pool1/18_vausdc_deposit_token.ts (L11-18)
```typescript
const func = buildDepositTokenDeployFunction({
  poolAlias: Pool1,
  underlyingAddress: VAUSDC_ADDRESS,
  underlyingSymbol: 'vaUSDC',
  underlyingDecimals: 18,
  collateralFactor: parseEther('0.6'), // 60%
  maxTotalSupply: parseEther('8000000'),
})
```

**File:** contracts/interfaces/external/IVPool.sol (L7-16)
```text
interface IVPool is IERC20 {
    function token() external view returns (address _token);

    function deposit(uint256 _amount) external;

    function withdraw(uint256 _shares) external;

    function poolRewards() external view returns (address);

    function pricePerShare() external view returns (uint256);
```

**File:** test/E2E.mainnet.next.test.ts (L420-439)
```typescript
    it('should deposit vaUSDC using USDC', async function () {
      //
      // Deploy `VesperGateway` implementation
      // Note: It won't be necessary when this contract get online
      //
      const vesperGatewayFactory = await ethers.getContractFactory('VesperGateway', alice)
      const vesperGateway = await vesperGatewayFactory.deploy(poolRegistry.address)

      // given
      const amount6 = parseUnits('1', 6)
      const before = await msdVaUSDC_1.balanceOf(alice.address)
      expect(before).eq(0)

      // when
      await usdc.approve(vesperGateway.address, amount6)
      await vesperGateway.deposit(pool_1.address, vaUSDC.address, amount6)

      // then
      const after = await msdVaUSDC_1.balanceOf(alice.address)
      expect(after).closeTo(parseUnits('0.77', 18), parseUnits('0.05', 18))
```
