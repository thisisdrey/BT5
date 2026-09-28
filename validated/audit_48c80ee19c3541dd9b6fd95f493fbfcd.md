### Title
Fee-on-transfer underlying tokens cannot be deposited through VesperGateway - (File: contracts/VesperGateway.sol)

### Summary
`VesperGateway.deposit()` performs a two-step transfer: it first `safeTransferFrom`s `amount_` of the vPool's `underlying` token from the caller to the gateway, then calls `vToken_.deposit(amount_)`, which internally pulls `amount_` from the gateway via `transferFrom`. The same `amount_` is reused for both hops. If `underlying` is a fee-on-transfer (FoT) token, the gateway receives less than `amount_` after step 1, so the vPool's pull in step 2 exceeds the gateway's balance and the whole transaction reverts.

Note the contrast: `DepositToken.deposit()` itself is FoT-safe because it measures the treasury's balance delta (`contracts/DepositToken.sol:225-227`), and `SmartFarmingManager.leverage()` is FoT-safe via `_safeTransferFrom` balance-delta accounting (`contracts/SmartFarmingManager.sol:257-261`). Only the gateway's intermediate hop breaks.

### Finding Description
In `contracts/VesperGateway.sol:44-65`:

```solidity
// 1. Get `underlying` asset
IERC20 _underlying = IERC20(vToken_.token());
_underlying.safeTransferFrom(_msgSender, address(this), amount_);   // gateway receives amount_ - fee

// 2. Deposit `underlying` to `VPool`
_underlying.safeApprove(address(vToken_), 0);
_underlying.safeApprove(address(vToken_), amount_);
uint256 _balanceBefore = vToken_.balanceOf(address(this));
vToken_.deposit(amount_);                                            // vPool pulls `amount_` -> insufficient balance -> revert
uint256 _vTokenAmount = vToken_.balanceOf(address(this)) - _balanceBefore;
```

Vesper `VPool.deposit(amount)` calls `token.transferFrom(msg.sender, address(this), amount)` on the underlying. Since the gateway only holds `amount_ - fee`, the ERC20 `transferFrom` reverts with insufficient balance, atomically reverting the entire `deposit()` call.

Attack/unprivileged trace: any EOA calls `VesperGateway.deposit(pool_, vToken_, amount_)` on a registered pool where `vToken_.token()` is a FoT token (attacker may supply their own FoT token arguments only to demonstrate; the bug applies to any FoT underlying registered as collateral through a Vesper pool). No privileged role is needed to trigger the revert.

### Impact Explanation
Deposits of a fee-on-transfer underlying through `VesperGateway` always revert — the exact "fee-on-transfer tokens cannot be used" impact from the reference report. Users cannot leverage the gateway's single-transaction underlying→vToken→msdToken flow for such tokens; they must manually acquire the vToken and call `DepositToken.deposit()` instead. No funds are stolen or permanently frozen (the revert returns tokens to the sender), so severity is bounded — a liveness/usability failure for a token class the rest of the codebase explicitly supports via balance-delta accounting.

### Likelihood Explanation
Occurs deterministically for any registered Vesper vToken whose `underlying` charges a transfer fee. It requires a pool to list a vToken whose underlying is FoT — the protocol's own `DepositToken.deposit()` already handles FoT tokens, indicating such collateral is intended to be supported. Trigger requires only a public call.

### Recommendation
Measure the actual received amount after step 1 and use it for the vPool deposit:

```solidity
uint256 _underlyingBefore = _underlying.balanceOf(address(this));
_underlying.safeTransferFrom(_msgSender, address(this), amount_);
uint256 _received = _underlying.balanceOf(address(this)) - _underlyingBefore;
_underlying.safeApprove(address(vToken_), 0);
_underlying.safeApprove(address(vToken_), _received);
vToken_.deposit(_received);
```

### Proof of Concept
Hardhat test sketch (mirroring the existing FoT deposit test in `test/DepositToken.test.ts:433` which uses `met.updateFee`):

```typescript
it('reverts when underlying has transfer fee', async function () {
    // given: FoT underlying with 10% fee
    await underlying.updateFee(parseEther('0.1'))
    await underlying.connect(alice).approve(vesperGateway.address, MaxUint256)

    // when: deposit 100 underlying -> gateway receives 90
    const tx = vesperGateway.connect(alice).deposit(pool.address, vToken.address, parseEther('100'))

    // then: vToken.deposit(100) pulls 100 from gateway holding only 90 -> revert
    await expect(tx).reverted // ERC20: transfer amount exceeds balance
})
```

Reproduction on fork: point `vToken_` at any Vesper VPool whose `token()` is a FoT asset (or a mock VPool wrapping a FoT ERC20), call `VesperGateway.deposit`, observe revert at the vPool's internal `transferFrom`.