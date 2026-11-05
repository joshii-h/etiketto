from zinklabel.device import nominal_width


def test_nominal_width():
    assert nominal_width(2.0 * 25.4) == 50
    assert nominal_width(0.5 * 25.4) == 12
    assert nominal_width(0.35 * 25.4) == 9
