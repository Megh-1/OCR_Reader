from app.utils.geometry import pixels_to_relative_bbox


def test_pixels_to_relative_bbox_is_exact():
    assert pixels_to_relative_bbox((100, 100, 500, 300), 1000, 500) == (0.1, 0.2, 0.5, 0.6)


def test_pixels_to_relative_bbox_clamps_out_of_bounds_pixels():
    # A box that (through margin padding, rounding, etc.) extends past the
    # image's own edges must not silently produce an out-of-range bbox -
    # 0.0-1.0 is a hard invariant the template schema enforces on write.
    assert pixels_to_relative_bbox((-20, -20, 1200, 600), 1000, 500) == (0.0, 0.0, 1.0, 1.0)
