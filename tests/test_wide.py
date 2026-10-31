from screener import wide


def test_sic_sectors():
    assert wide.sic_sector("1311") == "Energy"           # crude petroleum
    assert wide.sic_sector(1531) == "Consumer Discretionary"  # homebuilders
    assert wide.sic_sector(2834) == "Health Care"        # pharma
    assert wide.sic_sector(7372) == "Information Technology"  # software
    assert wide.sic_sector(4911) == "Utilities"
    assert wide.sic_sector(4953) == "Industrials"        # waste management
    assert wide.sic_sector(6022) == "Financials"
    assert wide.sic_sector(6798) == "Real Estate"
    assert wide.sic_sector(6770) is None                 # SPAC
    assert wide.sic_sector("") is None


def test_not_common():
    bad = ["ABCDW", "XYZ-WS", "ABC-U", "ABC-PA", "ABCDR"]
    good = ["ABC", "ABCD", "BRK-B", "GOOGL"]
    assert all(wide._NOT_COMMON.search(t) for t in bad)
    assert not any(wide._NOT_COMMON.search(t) for t in good)
