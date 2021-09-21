#  Copyright (c) 2021, CRS4
#
#  Permission is hereby granted, free of charge, to any person obtaining a copy of
#  this software and associated documentation files (the "Software"), to deal in
#  the Software without restriction, including without limitation the rights to
#  use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of
#  the Software, and to permit persons to whom the Software is furnished to do so,
#  subject to the following conditions:
#
#  The above copyright notice and this permission notice shall be included in all
#  copies or substantial portions of the Software.
#
#  THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
#  IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS
#  FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR
#  COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER
#  IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
#  CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

from math import ceil, log2
import os
import numpy as np
import tiledb
from lxml import etree
from PIL import Image
from copy import copy
import palettable.colorbrewer.sequential as palettes

from .dzi_adapter_interface import DZIAdapterInterface
from .errors import InvalidAttribute, InvalidColorPalette, InvalidTileAddress
from .. import settings


class TileDBDZIAdapter(DZIAdapterInterface):

    def __init__(self, tiledb_file, tiledb_repo):
        super(TileDBDZIAdapter, self).__init__()
        self.tiledb_resource = os.path.join(tiledb_repo, tiledb_file)
        self.logger.debug('TileDB adapter initialized')

    def _get_meta_attributes(self, keys):
        with tiledb.open(self.tiledb_resource) as A:
            attributes = dict()
            for k in keys:
                try:
                    attributes[k] = A.meta[k]
                except:
                    self.logger.error('Error when loading attribute %s' % k)
        return attributes

    def _get_meta_attribute(self, key):
        with tiledb.open(self.tiledb_resource) as A:
            try:
                return A.meta[key]
            except:
                self.logger.error('Error when loading attribute %s' % key)

    def _get_dataset_shape(self):
        with tiledb.open(self.tiledb_resource) as A:
            return A.shape

    def _get_schema(self):
        return tiledb.ArraySchema.load(self.tiledb_resource)

    def _check_attribute(self, attribute):
        schema = self._get_schema()
        return schema.has_attr(attribute)

    def _get_attribute_by_index(self, attribute_index):
        schema = self._get_schema()
        if attribute_index >= 0 and attribute_index < schema.nattr:
            return schema.attr(attribute_index).name
        else:
            raise IndexError('Schema has no attribute for index %d' % attribute_index)

    def _get_dzi_tile_coordinates(self, row, column, tile_size):
        x_min = row*tile_size
        y_min = column*tile_size
        x_max = x_min+tile_size
        y_max = y_min+tile_size
        return {
            'x_min': x_min,
            'x_max': x_max,
            'y_min': y_min,
            'y_max': y_max
        }

    def _get_dzi_level(self, shape):
        return int(ceil(log2(max(*shape))))
    
    def _get_dataset_dzi_dimensions(self, attribute):
        attrs = self._get_meta_attributes([
            'original_width', 'original_height',
            '{0}.dzi_sampling_level'.format(attribute),
            '{0}.tile_size'.format(attribute)
        ])
        dzi_max_level = self._get_dzi_level((attrs['original_width'], attrs['original_height']))
        dataset_shape = self._get_dataset_shape()
        zoom_scale_factor = pow(2, dzi_max_level-attrs['{0}.dzi_sampling_level'.format(attribute)])
        return {
            'width': dataset_shape[1]*attrs['{0}.tile_size'.format(attribute)]*zoom_scale_factor,
            'height': dataset_shape[0]*attrs['{0}.tile_size'.format(attribute)]*zoom_scale_factor
        }

    def _get_zoom_scale_factor(self, dzi_zoom_level, dataset_attribute):
        tiledb_zoom_level = self._get_meta_attribute('{0}.dzi_sampling_level'.format(dataset_attribute))
        return pow(2, (tiledb_zoom_level-dzi_zoom_level))

    def _get_dataset_tile_coordinates(self, dzi_coordinates, zoom_scale_factor):
        return {k:(v*zoom_scale_factor) for (k, v) in dzi_coordinates.items()}

    def _get_dataset_tiles(self, coordinates, dataset_attribute):
        dataset_tile_size = self._get_meta_attribute('{0}.tile_size'.format(dataset_attribute))
        col_min = int(coordinates['x_min']/dataset_tile_size)
        row_min = int(coordinates['y_min']/dataset_tile_size)
        col_max = ceil(coordinates['x_max']/dataset_tile_size)
        row_max = ceil(coordinates['y_max']/dataset_tile_size)
        return {
            'col_min': col_min,
            'col_max': col_max,
            'row_min': row_min,
            'row_max': row_max
        }

    def _slice_by_attribute(self, attribute, level, row, column, dzi_tile_size):
        dzi_coordinates = self._get_dzi_tile_coordinates(row, column, dzi_tile_size)
        zoom_scale_factor = self._get_zoom_scale_factor(level, attribute)
        dataset_tiles = self._get_dataset_tiles(
            self._get_dataset_tile_coordinates(dzi_coordinates, zoom_scale_factor),
            attribute
        )
        with tiledb.open(self.tiledb_resource) as A:
            q = A.query(attrs=(attribute,))
            try:
                data = q[dataset_tiles['col_min']:dataset_tiles['col_max'],
                         dataset_tiles['row_min']:dataset_tiles['row_max']][attribute]/100.
            except tiledb.TileDBError:
                raise InvalidTileAddress('Invalid address (%d,%d) for level %d' % (row, column, level))
        return data, zoom_scale_factor

    def _apply_palette(self, slice, palette):
        try:
            p_obj = getattr(palettes, palette)
        except AttributeError:
            raise InvalidColorPalette('%s is not a valid color palette' % palette)
        p_colors = copy(p_obj.colors)
        p_colors.insert(0, [255, 255, 255]) # TODO: check if actually necessary
        norm_slice = np.asarray(np.uint8(slice*len(p_colors))).reshape(-1)
        # extend the p_colors array to avoid an issue related to probabilities with a value of 1.0
        p_colors.append(p_colors[-1])
        colored_slice = [p_colors[int(y)] for y in norm_slice]
        return np.array(colored_slice).reshape(*slice.shape, 3)

    def _tile_to_img(self, tile, mode='RGB'):
        img = Image.fromarray(np.uint8(tile), mode)
        return img
    
    def _get_expected_tile_size(self, dzi_tile_size, zoom_scale_factor, dataset_tile_size):
        return max(int((dzi_tile_size*zoom_scale_factor)/dataset_tile_size), 1)

    def _slice_to_tile(self, slice, tile_size, zoom_scale_factor, dataset_tile_size, palette):
        expected_tile_size = self._get_expected_tile_size(tile_size, zoom_scale_factor, dataset_tile_size)
        tile = self._apply_palette(slice, palette)
        tile = self._tile_to_img(tile)
        self.logger.debug('Tile width: {0} --- Tile Height: {1}'.format(tile.width, tile.height))
        self.logger.debug('Expected tile size {0}'.format(expected_tile_size))
        return tile.resize(
            (
                int(tile_size*(tile.width/expected_tile_size)),
                int(tile_size*(tile.height/expected_tile_size))
            ), Image.BOX)

    def get_dzi_description(self, tile_size=None, attribute_label=None):
        if attribute_label is None:
            attribute = self._get_attribute_by_index(0)
        else:
            if self._check_attribute(attribute_label):
                attribute = attribute_label
            else:
                raise InvalidAttribute('Dataset has no attribute %s' % attribute_label)
        dset_dims = self._get_dataset_dzi_dimensions(attribute)
        tile_size = tile_size if tile_size is not None else settings.DEEPZOOM_TILE_SIZE
        dzi_root = etree.Element(
            'Image',
            attrib={
                'Format': 'png',
                'Overlap': '0', # no overlap when rendering array datasets
                'TileSize': str(tile_size)
            },
            nsmap={None: 'http://schemas.microsoft.com/deepzoom/2008'}
        )
        etree.SubElement(dzi_root, 'Size',
                        attrib={
                            'Height': str(dset_dims['height']),
                            'Width': str(dset_dims['width'])
                        })
        return etree.tostring(dzi_root)


    def get_tile(self, level, row, column, palette, attribute_label=None, tile_size=None):
        self.logger.debug('Loading tile')
        tile_size = tile_size if tile_size is not None else settings.DEEPZOOM_TILE_SIZE
        self.logger.debug('Setting tile size to %dpx', tile_size)
        if attribute_label is None:
            attribute = self._get_attribute_by_index(0)
        else:
            if self._check_attribute(attribute_label):
                attribute = attribute_label
            else:
                raise InvalidAttribute('Dataset has no attribute %s' % attribute_label)
        self.logger.debug('Slicing by attribute %s', attribute)
        slice, zoom_scale_factor = self._slice_by_attribute(attribute, int(level), int(row), int(column), tile_size)
        return self._slice_to_tile(slice, tile_size, zoom_scale_factor,
                                   self._get_meta_attribute('{0}.tile_size'.format(attribute)),
                                   palette)
