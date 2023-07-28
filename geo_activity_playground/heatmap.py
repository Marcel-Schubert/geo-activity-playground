# Copyright (c) 2018 Remi Salmon
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
import argparse
import dataclasses
import glob
import os
import pathlib
import tomllib

import matplotlib.pyplot as plt
import numpy as np

from geo_activity_playground.core.tiles import get_tile
from geo_activity_playground.core.tiles import latlon_to_xy
from geo_activity_playground.core.tiles import xy_to_latlon
from geo_activity_playground.strava.importing import read_all_activities

# globals
PLT_COLORMAP = "hot"  # matplotlib color map
MAX_TILE_COUNT = 2000  # maximum number of tiles to download
MAX_HEATMAP_SIZE = (2160, 3840)  # maximum heatmap size in pixel

OSM_TILE_SERVER = "https://maps.wikimedia.org/osm-intl/{}/{}/{}.png"  # OSM tile url from https://wiki.openstreetmap.org/wiki/Tile_servers
OSM_TILE_SIZE = 256  # OSM tile size in pixel
OSM_MAX_ZOOM = 19  # OSM maximum zoom level


def gaussian_filter(image, sigma):
    # returns image filtered with a gaussian function of variance sigma**2
    #
    # input: image = numpy.ndarray
    #        sigma = float
    # output: image = numpy.ndarray

    i, j = np.meshgrid(
        np.arange(image.shape[0]), np.arange(image.shape[1]), indexing="ij"
    )

    mu = (int(image.shape[0] / 2.0), int(image.shape[1] / 2.0))

    gaussian = (
        1.0
        / (2.0 * np.pi * sigma * sigma)
        * np.exp(-0.5 * (((i - mu[0]) / sigma) ** 2 + ((j - mu[1]) / sigma) ** 2))
    )

    gaussian = np.roll(gaussian, (-mu[0], -mu[1]), axis=(0, 1))

    image_fft = np.fft.rfft2(image)
    gaussian_fft = np.fft.rfft2(gaussian)

    image = np.fft.irfft2(image_fft * gaussian_fft)

    return image


@dataclasses.dataclass
class GeoBounds:
    lat_bound_min: float
    lat_bound_max: float
    lon_bound_min: float
    lon_bound_max: float


def render_heatmap(
    lat_lon_data: np.ndarray, num_activities: int, arg_zoom: int = -1
) -> np.ndarray:
    # find tiles coordinates
    lat_min, lon_min = np.min(lat_lon_data, axis=0)
    lat_max, lon_max = np.max(lat_lon_data, axis=0)

    if arg_zoom > -1:
        zoom = min(arg_zoom, OSM_MAX_ZOOM)

        x_tile_min, y_tile_max = map(int, latlon_to_xy(lat_min, lon_min, zoom))
        x_tile_max, y_tile_min = map(int, latlon_to_xy(lat_max, lon_max, zoom))

    else:
        zoom = OSM_MAX_ZOOM

        while True:
            x_tile_min, y_tile_max = map(int, latlon_to_xy(lat_min, lon_min, zoom))
            x_tile_max, y_tile_min = map(int, latlon_to_xy(lat_max, lon_max, zoom))

            if (x_tile_max - x_tile_min + 1) * OSM_TILE_SIZE <= MAX_HEATMAP_SIZE[
                0
            ] and (y_tile_max - y_tile_min + 1) * OSM_TILE_SIZE <= MAX_HEATMAP_SIZE[1]:
                break

            zoom -= 1

        print("Auto zoom = {}".format(zoom))

    tile_count = (x_tile_max - x_tile_min + 1) * (y_tile_max - y_tile_min + 1)

    if tile_count > MAX_TILE_COUNT:
        exit("ERROR zoom value too high, too many tiles to download")

    supertile = np.zeros(
        (
            (y_tile_max - y_tile_min + 1) * OSM_TILE_SIZE,
            (x_tile_max - x_tile_min + 1) * OSM_TILE_SIZE,
            3,
        )
    )

    n = 0
    for x in range(x_tile_min, x_tile_max + 1):
        for y in range(y_tile_min, y_tile_max + 1):
            n += 1

            tile = np.array(get_tile(zoom, x, y)) / 255

            i = y - y_tile_min
            j = x - x_tile_min

            supertile[
                i * OSM_TILE_SIZE : (i + 1) * OSM_TILE_SIZE,
                j * OSM_TILE_SIZE : (j + 1) * OSM_TILE_SIZE,
                :,
            ] = tile[:, :, :3]

    supertile = np.sum(supertile * [0.2126, 0.7152, 0.0722], axis=2)  # to grayscale
    supertile = 1.0 - supertile  # invert colors
    supertile = np.dstack((supertile, supertile, supertile))  # to rgb

    # fill trackpoints
    sigma_pixel = 1

    data = np.zeros(supertile.shape[:2])

    xy_data = latlon_to_xy(lat_lon_data[:, 0], lat_lon_data[:, 1], zoom)

    xy_data = np.array(xy_data).T
    xy_data = np.round(
        (xy_data - [x_tile_min, y_tile_min]) * OSM_TILE_SIZE
    )  # to supertile coordinates

    for j, i in xy_data.astype(int):
        data[
            i - sigma_pixel : i + sigma_pixel, j - sigma_pixel : j + sigma_pixel
        ] += 1.0

    res_pixel = (
        156543.03 * np.cos(np.radians(np.mean(lat_lon_data[:, 0]))) / (2.0**zoom)
    )  # from https://wiki.openstreetmap.org/wiki/Slippy_map_tilenames

    # trackpoint max accumulation per pixel = 1/5 (trackpoint/meter) * res_pixel (meter/pixel) * activities
    # (Strava records trackpoints every 5 meters in average for cycling activites)
    m = np.round((1.0 / 5.0) * res_pixel * num_activities)

    data[data > m] = m

    # equalize histogram and compute kernel density estimation
    data_hist, _ = np.histogram(data, bins=int(m + 1))

    data_hist = np.cumsum(data_hist) / data.size  # normalized cumulated histogram

    for i in range(data.shape[0]):
        for j in range(data.shape[1]):
            data[i, j] = m * data_hist[int(data[i, j])]  # histogram equalization

    data = gaussian_filter(
        data, float(sigma_pixel)
    )  # kernel density estimation with normal kernel

    data = (data - data.min()) / (data.max() - data.min())  # normalize to [0,1]

    # colorize
    cmap = plt.get_cmap(PLT_COLORMAP)

    data_color = cmap(data)
    data_color[data_color == cmap(0.0)] = 0.0  # remove background color

    for c in range(3):
        supertile[:, :, c] = (1.0 - data_color[:, :, c]) * supertile[
            :, :, c
        ] + data_color[:, :, c]

    print(np.min(supertile), np.max(supertile))

    return supertile


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate a PNG heatmap from local Strava GPX files",
        epilog="Report issues to https://github.com/remisalmon/Strava-local-heatmap/issues",
    )
    parser.add_argument(
        "--bounds",
        type=float,
        nargs=4,
        metavar="BOUND",
        default=[50.6570, 50.7896, 6.9979, 7.2136],
        help="heatmap bounding box as lat_min, lat_max, lon_min, lon_max (default: -90 +90 -180 +180)",
    )
    parser.add_argument(
        "--output", default="heatmap.png", help="heatmap name (default: heatmap.png)"
    )
    parser.add_argument(
        "--zoom",
        type=int,
        default=-1,
        help="heatmap zoom level 0-19 or -1 for auto (default: -1)",
    )
    parser.add_argument(
        "--sigma",
        type=int,
        default=1,
        help="heatmap Gaussian kernel sigma in pixel (default: 1)",
    )

    parser.add_argument("config_file", type=pathlib.Path)

    options = parser.parse_args()
    config_file: pathlib.Path = options.config_file

    with open(config_file, "rb") as f:
        config = tomllib.load(f)

    activities = read_all_activities()

    for heatmap_name, heatmap_spec in config["heatmaps"].items():
        bounds = GeoBounds(
            heatmap_spec["bottom"],
            heatmap_spec["top"],
            heatmap_spec["left"],
            heatmap_spec["right"],
        )
        selection = (
            (bounds.lat_bound_min < activities.Latitude)
            & (activities.Latitude < bounds.lat_bound_max)
            & (bounds.lon_bound_min < activities.Longitude)
            & (activities.Longitude < bounds.lon_bound_max)
        )
        filtered_points = activities.loc[selection]
        points = np.column_stack([filtered_points.Latitude, filtered_points.Longitude])
        print("Rendering Heatmap …")
        heatmap = render_heatmap(
            points, num_activities=len(filtered_points.Activity.unique())
        )
        output_filename = config_file.parent / f"Heatmap {heatmap_name}.png"
        plt.imsave(output_filename, heatmap)


if __name__ == "__main__":
    main()