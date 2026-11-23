import h3

H3_RESOLUTION = 8


def location_cell(latitude: float, longitude: float) -> str:
    return h3.latlng_to_cell(latitude, longitude, H3_RESOLUTION)
