### Title
Fee-on-transfer collateral causes users to receive less than their burned deposit shares on withdraw - (File: contracts/Treasury.sol)

### Summary
`DepositToken.deposit` accounts for fee-on-transfer (FoT) tokens using a balance-delta measurement, but the withdrawal path (`DepositToken._withdraw` → `Treasury.pull`) pushes a nominal amount with a plain `safeTransfer`. If a FoT token is whitelisted as collateral, every withdrawer receives less underlying than the msdTOKEN amount they burned. The same applies to `withdrawFrom` and `flashWithdraw` (SmartFarmingManager), which route through `_withdraw`.

### Finding Description
On deposit, `DepositToken.deposit` correctly measures what actually arrived at the Treasury:

```solidity
// contracts/DepositToken.sol
uint256 _balanceBefore = _underlying.balanceOf(_treasury);
_underlying.safeTransferFrom(_msgSender, _treasury, amount_);
amount_ = _underlying.balanceOf(_treasury) - _balanceBefore;
``` [1](#0-0) 

This shows the protocol intends to support FoT tokens: minted shares match the real collateral received.

On withdraw, `_withdraw` burns `_withdrawn` shares and asks the Treasury to send exactly `_withdrawn` underlying:

```solidity
(_withdrawn, _fee) = quoteWithdrawOut(amount_);
...
_burn(account_, _withdrawn);
_pool.treasury().pull(to_, _withdrawn);
``` [2](#0-1) 

`Treasury.pull` performs a nominal `safeTransfer` with no received-amount accounting or gross-up:

```solidity
IDepositToken(_msgSender).underlying().safeTransfer(to_, amount_);
``` [3](#0-2) 

With a FoT token (e.g., a token deducting a percentage on `transfer`), `to_` receives `_withdrawn - transferFee`, while the user's deposit position is debited the full `_withdrawn`. Contrast with `VesperGateway.withdraw`, which already measures `underlying` by balance delta before forwarding it, confirming the intended pattern is delta-based accounting for the external token.

### Impact Explanation
Any user withdrawing a FoT collateral token loses the transfer fee on every withdrawal — they burn shares worth `_withdrawn` but receive strictly less. Because shares are burned at face value while less collateral leaves, the loss compounds across users; and for flows that price collateral off `totalSupply` (e.g., `claimFromVesper`'s `_depositToken.totalSupply()` surplus check), accounting assumes a 1:1 backing that the real Treasury balance no longer satisfies. This is a direct loss of user funds with no compensating credit, identical in effect to the reported BlueBerry `withdrawLend` issue.

### Likelihood Explanation
Likelihood is conditional on a FoT token being added as collateral. Unlike BlueBerry, Metronome cannot reach this path with an attacker-chosen token — `Treasury.pull` only serves registered DepositTokens (`doesDepositTokenExist` check), and `PoolRegistry` registration is governance-gated. However, the explicit balance-delta handling in `deposit` and in `VesperGateway.withdraw` demonstrates FoT support is an intended design consideration, so the asymmetric handling on the withdraw side is a real defect rather than a purely hypothetical configuration. No modifier (`nonReentrant`, `whenNotShutdown`, `onlyIfDepositTokenExists`) prevents it.

### Recommendation
In `Treasury.pull` (or `DepositToken._withdraw`), either:
- Measure the recipient's balance delta and burn/charge accordingly, or
- Gross up the pull amount so `to_` receives exactly `_withdrawn` (e.g., `amount_ / (1 - fee)` where the fee is measurable via a test transfer or a registry of known fee rates), or
- Reject whitelisting of tokens whose `transfer` delivers less than `amount_` (enforceable via a deposit-time check that `amount_` sent equals delta received).

### Proof of Concept
Foundry fork sketch (deploy a mock FoT ERC20 that burns 1% on transfer, or fork a chain with PAXG/USDT-style tokens):

```solidity
function test_fotWithdrawShortfall() public {
    // 1. Governor whitelists FoT token T as collateral -> DepositToken dt
    // 2. Attacker/user deposits 100e18 T; deposit() measures delta -> mints 99e18 msdT
    uint256 balBefore = T.balanceOf(address(treasury));
    T.approve(address(dt), 100e18);
    (uint256 deposited,) = dt.deposit(100e18, alice);
    assertEq(deposited, 99e18); // FoT accounted on deposit

    // 3. User withdraws all shares
    (uint256 withdrawn,) = dt.withdraw(deposited, alice);

    // 4. Treasury.pull sends `withdrawn`, but T burns 1% again
    // alice receives ~0.99 * withdrawn < her burned shares
    assertLt(T.balanceOf(alice), withdrawn);
}
```

Uncertainty note: I could not verify whether any currently deployed underlying is FoT (deployment configs list standard collaterals), so the practical trigger depends on a FoT token being whitelisted. The code asymmetry (delta on deposit/gateway paths, nominal transfer on withdraw) is confirmed in the indexed sources.

### Citations

**File:** contracts/DepositToken.sol (L225-227)
```text
        uint256 _balanceBefore = _underlying.balanceOf(_treasury);
        _underlying.safeTransferFrom(_msgSender, _treasury, amount_);
        amount_ = _underlying.balanceOf(_treasury) - _balanceBefore;
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
