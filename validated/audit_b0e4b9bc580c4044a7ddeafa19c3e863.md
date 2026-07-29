[1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** tare-io__tare-contracts/contracts/LoansExchange.sol (L76-96)
```text
    require(msg.sender != buyer && buyer != address(0), InvalidBuyer());
    require(deadline > block.timestamp, InvalidDeadline());
    require(ILoansAuth(address(LOANS)).isRegisteredForRole(msg.sender, Roles.Investor, buyer), BuyerNotRegistered());

    offerId = ++offerCount;

    for (uint256 i = 0; i < loanIdsLength; ++i) {
      uint64 loanId = loanIds[i];
      require(LOANS_NFT.ownerOf(uint256(loanId)) == msg.sender, NotLoanOwner());
      require(LOANS_NFT.getLocked(uint256(loanId)) == address(0), LoanLocked());

      LOANS_NFT.lock(address(this), uint256(loanId));
    }

    _offers[offerId] = SaleOffer({
      seller: msg.sender,
      buyer: buyer,
      price: price,
      deadline: deadline,
      loanIds: loanIds
    });
```

**File:** tare-io__tare-contracts/contracts/LoansExchange.sol (L102-126)
```text
  function acceptOffer(uint64 offerId) external whenNotPaused nonReentrant {
    SaleOffer storage offer = _offers[offerId];

    // An inactive offer has `buyer == address(0)`, which `msg.sender` can never equal.
    require(msg.sender == offer.buyer, NotOfferRecipient());
    require(block.timestamp <= offer.deadline, OfferExpired());

    address seller = offer.seller;
    uint128 price = offer.price;

    require(ILoansAuth(address(LOANS)).isRegisteredForRole(seller, Roles.Investor, msg.sender), BuyerNotRegistered());
    require(ILoansAuth(address(LOANS)).isRegisteredForRole(msg.sender, Roles.Investor, seller), SellerNotRegistered());

    uint64[] memory loanIds = _removeOffer(offerId);

    // Send Loan NFTs to the buyer first, before any cash moves.
    uint256 loanIdsLength = loanIds.length;
    for (uint256 i = 0; i < loanIdsLength; ++i) {
      LOANS_NFT.transferFrom(seller, msg.sender, uint256(loanIds[i]));
    }

    // Pull currency from the buyer to the seller last.
    if (price > 0) {
      CURRENCY.safeTransferFrom(msg.sender, seller, uint256(price));
    }
```

**File:** tare-io__tare-contracts/contracts/LoansExchange.sol (L142-149)
```text
  function cancelOffer(uint64 offerId) external whenNotPaused nonReentrant {
    require(_offers[offerId].seller == msg.sender, NotSeller());

    uint64[] memory loanIds = _removeOffer(offerId);
    _unlockLoans(loanIds);

    emit OfferCancelled(offerId);
  }
```

**File:** tare-io__tare-contracts/contracts/LoansExchange.sol (L157-165)
```text
  function _removeOffer(uint64 offerId) internal returns (uint64[] memory loanIds) {
    SaleOffer storage offer = _offers[offerId];

    require(offer.buyer != address(0), OfferInactive());

    loanIds = offer.loanIds;

    delete _offers[offerId];
  }
```

**File:** tare-io__tare-contracts/contracts/misc/LoansAuth.sol (L30-42)
```text
  /// @inheritdoc ILoansAuth
  function registerAddress(Roles role, address addr) external {
    // forge-lint: disable-next-line(incorrect-shift)
    addressBook[msg.sender][addr] |= (1 << uint8(role));
    emit AddressRegistered(msg.sender, role, addr);
  }

  /// @inheritdoc ILoansAuth
  function unregisterAddress(Roles role, address addr) external {
    // forge-lint: disable-next-line(incorrect-shift)
    addressBook[msg.sender][addr] &= ~(1 << uint8(role));
    emit AddressUnregistered(msg.sender, role, addr);
  }
```
