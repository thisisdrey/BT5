After a thorough investigation of the Metric OMM price-provider and factory code, I traced every path the external bug class maps to:

**Bug class mapped:** stale/reused state assumption causes wrong anchor point → bad-price execution reaches pool swaps.

**