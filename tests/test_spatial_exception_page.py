from tomorrowcities.pages import spatial_exceptions


def test_spatial_exception_page_exposes_solara_page():
    assert callable(spatial_exceptions.Page)
